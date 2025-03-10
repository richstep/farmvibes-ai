import json
import os
import time
import warnings
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, TypeVar, Union, cast, overload
from urllib.parse import urljoin

import requests
import yaml
from dateutil.parser import ParserError, parse
from dateutil.tz import tzlocal
from dateutil.tz.tz import tzfile
from requests.exceptions import HTTPError
from shapely import geometry as shpg
from shapely.geometry.base import BaseGeometry

from vibe_core.data import BaseVibeDict, StacConverter
from vibe_core.data.core_types import BaseVibe
from vibe_core.data.json_converter import dump_to_json
from vibe_core.data.utils import deserialize_stac, serialize_input
from vibe_core.datamodel import (
    RunConfigInput,
    RunConfigUser,
    RunDetails,
    RunStatus,
    SpatioTemporalJson,
    TaskDescription,
)
from vibe_core.monitor import VibeWorkflowDocumenter, VibeWorkflowRunMonitor
from vibe_core.utils import ensure_list, format_double_escaped

FALLBACK_SERVICE_URL = "http://192.168.49.2:30000/"
XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
FARMVIBES_AI_SERVICE_URL_PATH = os.path.join(XDG_CONFIG_HOME, "farmvibes-ai", "service_url")
FARMVIBES_AI_REMOTE_SERVICE_URL_PATH = os.path.join(
    XDG_CONFIG_HOME, "farmvibes-ai", "remote_service_url"
)
DISK_FREE_THRESHOLD_BYTES = 50 * 1024 * 1024 * 1024  # 50 GiB


TASK_SORT_KEY = "submission_time"
T = TypeVar("T", bound=BaseVibe, covariant=True)
InputData = Union[Dict[str, Union[T, List[T]]], List[T], T]


class WorkflowRun(ABC):
    @property
    @abstractmethod
    def status(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def output(self) -> BaseVibeDict:
        raise NotImplementedError


class Client(ABC):
    @abstractmethod
    def list_workflows(self) -> List[str]:
        raise NotImplementedError

    @abstractmethod
    def run(
        self,
        workflow: str,
        geometry: BaseGeometry,
        time_range: Tuple[datetime, datetime],
    ) -> WorkflowRun:
        raise NotImplementedError


class FarmvibesAiClient(Client):
    default_headers: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    def __init__(self, baseurl: str):
        self.baseurl = baseurl
        self.session = requests.Session()
        self.session.headers.update(self.default_headers)

    def _request(self, method: str, endpoint: str, *args: Any, **kwargs: Any):
        response = self.session.request(method, urljoin(self.baseurl, endpoint), *args, **kwargs)
        try:
            r = json.loads(response.text)
        except json.JSONDecodeError:
            r = response.text
        try:
            response.raise_for_status()
        except HTTPError as e:
            error_message = r.get("message", "") if isinstance(r, dict) else r
            msg = f"{e}. {error_message}"
            raise HTTPError(msg, response=e.response)
        return cast(Any, r)

    def _form_payload(
        self,
        workflow: Union[str, Dict[str, Any]],
        parameters: Optional[Dict[str, Any]],
        geometry: Optional[BaseGeometry],
        time_range: Optional[Tuple[datetime, datetime]],
        input_data: Optional[InputData[T]],
        run_name: str,
    ) -> Dict[str, Any]:
        if input_data is not None:
            user_input = serialize_input(input_data)
        elif geometry is None or time_range is None:
            raise ValueError("Either `input_data` or `geometry` and `time_range` are required")
        else:
            geojson = {
                "features": [{"geometry": None, "type": "Feature"}],
                "type": "FeatureCollection",
            }
            geojson["features"][0]["geometry"] = shpg.mapping(geometry)
            user_input = SpatioTemporalJson(time_range[0], time_range[1], geojson)
        return asdict(RunConfigInput(run_name, workflow, parameters, user_input))

    def verify_disk_space(self):
        metrics = self.get_system_metrics()
        df = cast(Optional[int], metrics.get("disk_free", None))
        if df is not None and df < DISK_FREE_THRESHOLD_BYTES:
            warnings.warn(
                "The FarmVibes.AI cache is running low on disk space "
                f"and only has {df / 1024 / 1024 / 1024} GiB left. "
                "Please consider clearing the cache to free up space and "
                "to avoid potential failures.",
                category=RuntimeWarning,
            )

    def list_workflows(self) -> List[str]:
        return self._request("GET", "v0/workflows")

    def describe_workflow(self, workflow_name: str) -> Dict[str, Any]:
        desc = self._request("GET", f"v0/workflows/{workflow_name}?return_format=description")
        desc["description"] = TaskDescription(**desc["description"])
        return desc

    def get_system_metrics(self) -> Dict[str, Union[int, float, Tuple[float, ...]]]:
        return self._request("GET", "v0/system-metrics")

    def get_workflow_yaml(self, workflow_name: str) -> str:
        yaml_content = self._request("GET", f"v0/workflows/{workflow_name}?return_format=yaml")
        return yaml.dump(yaml_content, default_flow_style=False, default_style="", sort_keys=False)

    def cancel_run(self, run_id: str) -> str:
        return self._request("POST", f"v0/runs/{run_id}/cancel")["message"]

    def describe_run(self, run_id: str) -> RunConfigUser:
        return RunConfigUser(**self._request("GET", f"v0/runs/{run_id}"))

    def document_workflow(self, workflow_name: str) -> None:
        wf_dict = self.describe_workflow(workflow_name)

        documenter = VibeWorkflowDocumenter(
            name=workflow_name,
            sources=wf_dict["inputs"],
            sinks=wf_dict["outputs"],
            parameters=wf_dict["parameters"],
            description=wf_dict["description"],
        )

        documenter.print_documentation()

    def list_runs(
        self,
        ids: Optional[Union[str, List[str]]] = None,
        fields: Optional[Union[str, List[str]]] = None,
    ):
        ids = [f"ids={id}" for id in ensure_list(ids)] if ids is not None else []
        fields = [f"fields={field}" for field in ensure_list(fields)] if fields is not None else []
        query_str = "&".join(ids + fields)

        return self._request("GET", f"v0/runs?{query_str}")

    def get_run_by_id(self, id: str) -> "VibeWorkflowRun":
        fields = ["id", "name", "workflow", "parameters"]
        run = self.list_runs(id, fields=fields)[0]
        return VibeWorkflowRun(*(run[f] for f in fields), self)  # type: ignore

    def get_api_time_zone(self) -> tzfile:
        tz = tzlocal()
        response = self.session.request("GET", self.baseurl)
        try:
            dt = parse(response.headers["date"])
            tz = dt.tzinfo if dt.tzinfo is not None else tzlocal()
        except KeyError:
            warnings.warn(
                "Could not determine the time zone of the FarmVibes.AI REST-API. "
                "'date' header is missing from the response. "
                "Using the client time zone instead.",
                category=RuntimeWarning,
            )
        except ParserError:
            warnings.warn(
                "Could not determine the time zone of the FarmVibes.AI REST-API. "
                "Unable to parse the 'date' header from the response. "
                "Using the client time zone instead.",
                category=RuntimeWarning,
            )
        return cast(tzfile, tz)

    @overload
    def run(
        self,
        workflow: Union[str, Dict[str, Any]],
        name: str,
        *,
        geometry: BaseGeometry,
        time_range: Tuple[datetime, datetime],
        parameters: Optional[Dict[str, Any]] = None,
    ) -> "VibeWorkflowRun":
        ...

    @overload
    def run(
        self,
        workflow: Union[str, Dict[str, Any]],
        name: str,
        *,
        input_data: InputData[T],
        parameters: Optional[Dict[str, Any]] = None,
    ) -> "VibeWorkflowRun":
        ...

    def run(
        self,
        workflow: Union[str, Dict[str, Any]],
        name: str,
        *,
        geometry: Optional[BaseGeometry] = None,
        time_range: Optional[Tuple[datetime, datetime]] = None,
        input_data: Optional[InputData[T]] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> "VibeWorkflowRun":
        self.verify_disk_space()
        payload = dump_to_json(
            self._form_payload(workflow, parameters, geometry, time_range, input_data, name),
        )
        response = self._request("POST", "v0/runs", data=payload)
        return self.get_run_by_id(response["id"])

    def resubmit_run(self, run_id: str) -> "VibeWorkflowRun":
        """
        Resubmits a workflow run with the given run ID.

        :param run_id: The ID of the workflow run to resubmit.

        :return: The resubmitted workflow run.
        """

        self.verify_disk_space()
        response = self._request("POST", f"v0/runs/{run_id}/resubmit")
        return self.get_run_by_id(response["id"])


class VibeWorkflowRun(WorkflowRun):
    wait_s = 10

    def __init__(
        self,
        id: str,
        name: str,
        workflow: str,
        parameters: Dict[str, Any],
        client: FarmvibesAiClient,
    ):
        self.id = id
        self.name = name
        self.workflow = workflow
        self.parameters = parameters
        self.client = client
        self._status = RunStatus.pending
        self._reason = ""
        self._output = None
        self._task_details = None

    def _convert_output(self, output: Dict[str, Any]) -> BaseVibeDict:
        converter = StacConverter()
        return {k: converter.from_stac_item(deserialize_stac(v)) for k, v in output.items()}

    def _convert_task_details(self, details: Dict[str, Any]) -> Dict[str, RunDetails]:
        return {
            k: RunDetails(**v)
            for k, v in sorted(
                details.items(),
                key=lambda x: cast(
                    datetime, parse(x[1][TASK_SORT_KEY]) if x[1][TASK_SORT_KEY] else datetime.now()
                ),
            )
        }

    @property
    def status(self) -> RunStatus:
        if not RunStatus.finished(self._status):
            self._status = RunStatus(self.client.list_runs(self.id)[0]["details.status"])
        return self._status

    @property
    def task_details(self) -> Dict[str, RunDetails]:
        if self._task_details is not None:
            return self._task_details
        status = self.status
        task_details = self._convert_task_details(
            self.client.list_runs(ids=self.id, fields="task_details")[0]["task_details"]
        )
        if RunStatus.finished(status):
            self._task_details = task_details
        return task_details

    @property
    def task_status(self) -> Dict[str, str]:
        details = self.task_details
        return {k: v.status for k, v in details.items()}

    @property
    def output(self) -> Optional[BaseVibeDict]:
        if self._output is not None:
            return self._output
        run = self.client.describe_run(self.id)
        self._status = run.details.status
        if self._status != RunStatus.done:
            return None
        self._output = self._convert_output(run.output)
        return self._output

    @property
    def reason(self) -> Optional[str]:
        status = self.status
        if status == RunStatus.done:
            self._reason = "Workflow run was successful."
        elif status in [RunStatus.cancelled, RunStatus.cancelling]:
            self._reason = f"Workflow run {status}."
        elif status in [RunStatus.running, RunStatus.pending]:
            self._reason = (
                f"Workflow run is {status}. "
                f"Check {self.__class__.__name__}.monitor() for task updates."
            )
        else:  # RunStatus.failed
            run = self.client.list_runs(self.id, "details.reason")[0]
            self._reason = format_double_escaped(run["details.reason"])
        return self._reason

    def cancel(self) -> "VibeWorkflowRun":
        self.client.cancel_run(self.id)
        self.status
        return self

    def resubmit(self) -> "VibeWorkflowRun":
        """
        Resubmits the current workflow run.

        :return: The resubmitted workflow run instance.
        """
        return self.client.resubmit_run(self.id)

    def block_until_complete(self, timeout_s: Optional[int] = None) -> "VibeWorkflowRun":
        time_start = time.time()
        while self.status not in (RunStatus.done, RunStatus.failed):
            time.sleep(self.wait_s)
            if timeout_s is not None and (time.time() - time_start) > timeout_s:
                raise RuntimeError(
                    f"Timeout of {timeout_s}s reached while waiting for workflow completion"
                )
        return self

    def monitor(
        self,
        refresh_time_s: int = 1,
        refresh_warnings_time_min: int = 5,
        timeout_min: Optional[int] = None,
    ):
        """
        This method will block and print the status of the run each refresh_time_s seconds,
        until the workflow run finishes or it reaches timeout_min minutes
        """
        with warnings.catch_warnings(record=True) as monitored_warnings:
            monitor = VibeWorkflowRunMonitor(api_time_zone=self.client.get_api_time_zone())

            stop_monitoring = False
            time_start = last_warning_refresh = time.time()

            with monitor.live_context:
                while not stop_monitoring:
                    monitor.update_run_status(
                        self.workflow,
                        self.name,
                        self.id,
                        self.status,
                        self.task_details,
                        [w.message for w in monitored_warnings],
                    )

                    time.sleep(refresh_time_s)
                    curent_time = time.time()

                    # Check for warnings every refresh_warnings_time_min minutes
                    if (curent_time - last_warning_refresh) / 60.0 > refresh_warnings_time_min:
                        self.client.verify_disk_space()
                        last_warning_refresh = curent_time

                    # Check for timeout
                    did_timeout = (
                        timeout_min is not None and (curent_time - time_start) / 60.0 > timeout_min
                    )
                    stop_monitoring = RunStatus.finished(self.status) or did_timeout

                # Update one last time to make sure we have the latest state
                monitor.update_run_status(
                    self.workflow,
                    self.name,
                    self.id,
                    self.status,
                    self.task_details,
                    [w.message for w in monitored_warnings],
                )

    def __repr__(self):
        return (
            f"'{self.__class__.__name__}'(id='{self.id}', name='{self.name}',"
            f" workflow='{self.workflow}', status='{self.status}')"
        )


def get_local_service_url() -> str:
    try:
        with open(FARMVIBES_AI_SERVICE_URL_PATH, "r") as fp:
            return fp.read().strip()
    except FileNotFoundError:
        return FALLBACK_SERVICE_URL


def get_remote_service_url() -> str:
    try:
        with open(FARMVIBES_AI_REMOTE_SERVICE_URL_PATH, "r") as fp:
            return fp.read().strip()
    except FileNotFoundError as e:
        print(e)
        raise


def get_default_vibe_client(url: str = "", connect_remote: bool = False):
    if not url:
        url = get_remote_service_url() if connect_remote else get_local_service_url()

    return FarmvibesAiClient(url)
