import datetime
import json
import logging
import os

import flask
import flask_admin
import flask_appbuilder
import pendulum
import pkg_resources
import six

from airflow.configuration import conf
from airflow.jobs import BaseJob
from airflow.models import BaseOperator, DagModel, DagRun, XCom
from airflow.plugins_manager import AirflowPlugin
from airflow.utils.db import provide_session
from airflow.utils.net import get_hostname
from airflow.utils.timezone import utcnow
from airflow.version import version as airflow_version
from flask import Response
from sqlalchemy import and_, or_


DEFAULT_DAYS_PERIOD = 30
TASK_ARG_TYPES = (str, float, bool, int, datetime.datetime)

current_dags = {}

try:
    # in dbnd it might be overridden
    from airflow.models import original_TaskInstance as TaskInstance
except Exception:
    from airflow.models import TaskInstance


### Exceptions ###


class EmptyAirflowDatabase(Exception):
    pass


### Plugin Business Logic ###


def _load_dags_models(session=None):
    dag_models = session.query(DagModel).all()
    for dag_model in dag_models:
        current_dags[dag_model.dag_id] = dag_model


def do_export_data(
    dagbag,
    since,
    include_logs=False,
    include_task_args=False,
    include_xcom=False,
    dag_ids=None,
    task_quantity=None,
    session=None,
):
    """
    Get first task instances which have the largest amount of objects in DB.
    Then get related DAG runs and DAG runs in the same time frame.
    All DAGs are always exported since their amount is low.
    Amount of exported data is limited by tasks parameter which limits the number to task instances to export.
    """
    since = since or pendulum.datetime.min
    _load_dags_models(session)
    logging.info(
        "Collected %d dags. Trying to query task instances and dagruns from %s",
        len(current_dags),
        since,
    )

    task_instances, dag_runs = _get_task_instances(
        since, dag_ids, task_quantity, session
    )
    logging.info("%d task instances were found." % len(task_instances))

    task_end_dates = [
        task.end_date for task, job in task_instances if task.end_date is not None
    ]
    if not task_end_dates or not task_quantity or len(task_instances) < task_quantity:
        dag_run_end_date = pendulum.datetime.max
    else:
        dag_run_end_date = max(task_end_dates)

    dag_runs |= _get_dag_runs(since, dag_run_end_date, dag_ids, session)
    logging.info("%d dag runs were found." % len(dag_runs))

    if not task_instances and not dag_runs:
        return ExportData(since=since)

    dag_models = [d for d in current_dags.values() if d]
    if dag_ids:
        dag_models = [dag for dag in dag_models if dag.dag_id in dag_ids]

    xcom_results = (
        _get_full_xcom_dict(session, dag_ids, task_instances) if include_xcom else None
    )

    task_instances_result = []
    for ti, job in task_instances:
        if ti is None:
            continue
        for dag in [dagbag.get_dag(ti.dag_id)]:
            if dag is None:
                continue
            result = ETaskInstance.from_task_instance(
                ti,
                include_logs,
                dag.get_task(ti.task_id) if dag and dag.has_task(ti.task_id) else None,
                _get_task_instance_xcom_dict(
                    xcom_results, dag.dag_id, ti.task_id, ti.execution_date
                )
                if include_xcom
                else {},
            )
            task_instances_result.append(result)

    ed = ExportData(
        task_instances=task_instances_result,
        dag_runs=[EDagRun.from_dagrun(dr) for dr in dag_runs],
        dags=[
            EDag.from_dag(
                dagbag.get_dag(dm.dag_id), dagbag.dag_folder, include_task_args
            )
            for dm in dag_models
            if dagbag.get_dag(dm.dag_id)
        ],
        since=since,
    )

    return ed


def _get_dag_runs(start_date, end_date, dag_ids, session):
    dagruns_query = session.query(DagRun).filter(
        or_(
            DagRun.end_date.is_(None),
            and_(DagRun.end_date >= start_date, DagRun.end_date <= end_date),
        )
    )

    if dag_ids:
        dagruns_query = dagruns_query.filter(DagRun.dag_id.in_(dag_ids))

    return set(dagruns_query.all())


def _get_task_instances(start_date, dag_ids, quantity, session):
    task_instances_query = (
        session.query(TaskInstance, BaseJob, DagRun)
        .outerjoin(BaseJob, TaskInstance.job_id == BaseJob.id)
        .join(
            DagRun,
            (TaskInstance.dag_id == DagRun.dag_id)
            & (TaskInstance.execution_date == DagRun.execution_date),
        )
        .filter(
            or_(TaskInstance.end_date.is_(None), TaskInstance.end_date > start_date)
        )
    )

    if dag_ids:
        task_instances_query = task_instances_query.filter(
            TaskInstance.dag_id.in_(dag_ids)
        )

    if quantity is not None:
        task_instances_query = task_instances_query.order_by(
            TaskInstance.end_date
        ).limit(quantity)

    results = task_instances_query.all()
    tasks_and_jobs = [(task, job) for task, job, dag_run in results]
    dag_runs = {dag_run for task, job, dag_run in results}
    return tasks_and_jobs, dag_runs


def _get_full_xcom_dict(session, dag_ids, task_instances):
    xcom_query = session.query(XCom)
    if dag_ids:
        xcom_query = xcom_query.filter(XCom.dag_id.in_(dag_ids))

    task_ids = [ti.task_id for ti, job in task_instances]
    xcom_query = xcom_query.filter(XCom.task_id.in_(task_ids))

    results = xcom_query.all()
    if not results:
        return None

    xcom_results = {}
    for result in results:
        if result.dag_id not in xcom_results:
            xcom_results[result.dag_id] = {}
        if result.task_id not in xcom_results[result.dag_id]:
            xcom_results[result.dag_id][result.task_id] = {}
        if result.execution_date not in xcom_results[result.dag_id][result.task_id]:
            xcom_results[result.dag_id][result.task_id][result.execution_date] = {}
        xcom_results[result.dag_id][result.task_id][result.execution_date][
            result.key
        ] = str(result.value)

    return xcom_results


def _get_task_instance_xcom_dict(xcom_results, dag_id, task_id, execution_date):
    if not xcom_results:
        return {}

    if dag_id in xcom_results:
        if task_id in xcom_results[dag_id]:
            if execution_date in xcom_results[dag_id][task_id]:
                return xcom_results[dag_id][task_id][execution_date]

    return {}


@provide_session
def _get_current_dag_model(dag_id, session=None):
    # Optimize old DagModel.get_current to try cache first
    if dag_id not in current_dags:
        current_dags[dag_id] = (
            session.query(DagModel).filter(DagModel.dag_id == dag_id).first()
        )

    return current_dags[dag_id]


class ETask(object):
    def __init__(
        self,
        upstream_task_ids=None,
        downstream_task_ids=None,
        task_type=None,
        task_source_code=None,
        task_module_code=None,
        dag_id=None,
        task_id=None,
        retries=None,
        command=None,
        task_args=None,
    ):
        self.upstream_task_ids = list(upstream_task_ids)  # type: List[str]
        self.downstream_task_ids = list(downstream_task_ids)  # type: List[str]
        self.task_type = task_type
        self.task_source_code = task_source_code
        self.task_module_code = task_module_code
        self.dag_id = dag_id
        self.task_id = task_id
        self.retries = retries
        self.command = command
        self.task_args = task_args

    @staticmethod
    def from_task(t, include_task_args):
        # type: (BaseOperator) -> ETask
        return ETask(
            upstream_task_ids=t.upstream_task_ids,
            downstream_task_ids=t.downstream_task_ids,
            task_type=t.task_type,
            task_source_code=_get_source_code(t),
            task_module_code=_get_module_code(t),
            dag_id=t.dag_id,
            task_id=t.task_id,
            retries=t.retries,
            command=_get_command_from_operator(t),
            task_args=_extract_args_from_dict(vars(t)) if include_task_args else {},
        )

    def as_dict(self):
        return dict(
            upstream_task_ids=self.upstream_task_ids,
            downstream_task_ids=self.downstream_task_ids,
            task_type=self.task_type,
            task_source_code=self.task_source_code,
            task_module_code=self.task_module_code,
            dag_id=self.dag_id,
            task_id=self.task_id,
            retries=self.retries,
            command=self.command,
            task_args=self.task_args,
        )


class ETaskInstance(object):
    def __init__(
        self,
        execution_date,
        dag_id,
        state,
        try_number,
        task_id,
        start_date,
        end_date,
        log_body,
        xcom_dict,
    ):
        self.execution_date = execution_date
        self.dag_id = dag_id
        self.state = state
        self.try_number = try_number
        self.task_id = task_id
        self.start_date = start_date
        self.end_date = end_date
        self.log_body = log_body
        self.xcom_dict = xcom_dict

    @staticmethod
    def from_task_instance(ti, include_logs=False, task=None, xcom_dict=None):
        # type: (TaskInstance, bool, BaseOperator, dict) -> ETaskInstance
        return ETaskInstance(
            execution_date=ti.execution_date,
            dag_id=ti.dag_id,
            state=ti.state,
            try_number=ti._try_number,
            task_id=ti.task_id,
            start_date=ti.start_date,
            end_date=ti.end_date,
            log_body=_get_log(ti, task) if include_logs else None,
            xcom_dict=xcom_dict,
        )

    def as_dict(self):
        return dict(
            execution_date=self.execution_date,
            dag_id=self.dag_id,
            state=self.state,
            try_number=self.try_number,
            task_id=self.task_id,
            start_date=self.start_date,
            end_date=self.end_date,
            log_body=self.log_body,
            xcom_dict=self.xcom_dict,
        )


### Models ###


class EDagRun(object):
    def __init__(
        self, dag_id, dagrun_id, start_date, state, end_date, execution_date, task_args
    ):
        self.dag_id = dag_id
        self.dagrun_id = dagrun_id
        self.start_date = start_date
        self.state = state
        self.end_date = end_date
        self.execution_date = execution_date
        self.task_args = task_args

    @staticmethod
    def from_dagrun(dr):
        # type: (DagRun) -> EDagRun
        return EDagRun(
            dag_id=dr.dag_id,
            dagrun_id=dr.id,  # ???
            start_date=dr.start_date,
            state=dr.state,
            end_date=dr.end_date,
            execution_date=dr.execution_date,
            task_args=_extract_args_from_dict(dr.conf) if dr.conf else {},
        )

    def as_dict(self):
        return dict(
            dag_id=self.dag_id,
            dagrun_id=self.dagrun_id,
            start_date=self.start_date,
            state=self.state,
            end_date=self.end_date,
            execution_date=self.execution_date,
            task_args=self.task_args,
        )


class EDag(object):
    def __init__(
        self,
        description,
        root_task_ids,
        tasks,
        owner,
        dag_id,
        schedule_interval,
        catchup,
        start_date,
        end_date,
        is_committed,
        git_commit,
        dag_folder,
        hostname,
        source_code,
        is_subdag,
        task_type,
        task_args,
    ):
        self.description = description
        self.root_task_ids = root_task_ids  # type: List[str]
        self.tasks = tasks  # type: List[ETask]
        self.owner = owner
        self.dag_id = dag_id
        self.schedule_interval = schedule_interval
        self.catchup = catchup
        self.start_date = start_date
        self.end_date = end_date
        self.is_committed = is_committed
        self.git_commit = git_commit
        self.dag_folder = dag_folder
        self.hostname = hostname
        self.source_code = source_code
        self.is_subdag = is_subdag
        self.task_type = task_type
        self.task_args = task_args

    @staticmethod
    def from_dag(dag, dag_folder, include_task_args):
        # type: (DAG, str) -> EDag
        git_commit, git_committed = _get_git_status(dag_folder)
        return EDag(
            description=dag.description,
            root_task_ids=[t.task_id for t in dag.roots],
            tasks=[ETask.from_task(t, include_task_args) for t in dag.tasks],
            owner=dag.owner,
            dag_id=dag.dag_id,
            schedule_interval=interval_to_str(dag.schedule_interval),
            catchup=dag.catchup,
            start_date=dag.start_date or utcnow(),
            end_date=dag.end_date,
            is_committed=git_committed,
            git_commit=git_commit or "",
            dag_folder=dag_folder,
            hostname=get_hostname(),
            source_code=_read_dag_file(dag.fileloc),
            is_subdag=dag.is_subdag,
            task_type="DAG",
            task_args=_extract_args_from_dict(vars(dag)) if include_task_args else {},
        )

    def as_dict(self):
        return dict(
            description=self.description,
            root_task_ids=self.root_task_ids,
            tasks=[t.as_dict() for t in self.tasks],
            owner=self.owner,
            dag_id=self.dag_id,
            schedule_interval=self.schedule_interval,
            catchup=self.catchup,
            start_date=self.start_date,
            end_date=self.end_date,
            is_committed=self.is_committed,
            git_commit=self.git_commit,
            dag_folder=self.dag_folder,
            hostname=self.hostname,
            source_code=self.source_code,
            is_subdag=self.is_subdag,
            task_type=self.task_type,
            task_args=self.task_args,
        )


class ExportData(object):
    def __init__(self, since, dags=None, dag_runs=None, task_instances=None):
        self.dags = dags or []  # type: List[EDag]
        self.dag_runs = dag_runs or []  # type: List[EDagRun]
        self.task_instances = task_instances or []  # type: List[ETaskInstance]
        self.since = since  # type: Datetime
        self.airflow_version = airflow_version
        self.dags_path = conf.get("core", "dags_folder")
        self.logs_path = conf.get("core", "base_log_folder")
        self.airflow_export_version = _get_export_plugin_version()
        self.rbac_enabled = conf.get("webserver", "rbac")

    def as_dict(self):
        return dict(
            dags=[x.as_dict() for x in self.dags],
            dag_runs=[x.as_dict() for x in self.dag_runs],
            task_instances=[x.as_dict() for x in self.task_instances],
            since=self.since,
            airflow_version=self.airflow_version,
            dags_path=self.dags_path,
            logs_path=self.logs_path,
            airflow_export_version=self.airflow_export_version,
            rbac_enabled=self.rbac_enabled,
        )


### Helpers ###


def interval_to_str(schedule_interval):
    if isinstance(schedule_interval, datetime.timedelta):
        if schedule_interval == datetime.timedelta(days=1):
            return "@daily"
        if schedule_interval == datetime.timedelta(hours=1):
            return "@hourly"
    return str(schedule_interval)


def _get_log(ti, task):
    try:
        ti.task = task
        logger = logging.getLogger("airflow.task")
        task_log_reader = conf.get("core", "task_log_reader")
        handler = next(
            (handler for handler in logger.handlers if handler.name == task_log_reader),
            None,
        )
        logs, metadatas = handler.read(ti, ti._try_number, metadata={})
        return logs[0] if logs else None
    except Exception as e:
        pass
    finally:
        del ti.task


def _get_git_status(path):
    try:
        from git import Repo

        if os.path.isfile(path):
            path = os.path.dirname(path)

        repo = Repo(path, search_parent_directories=True)
        commit = repo.head.commit.hexsha
        return commit, not repo.is_dirty()
    except Exception as ex:
        return None, False


def _get_source_code(t):
    # type: (BaseOperator) -> str
    # TODO: add other "code" extractions
    # TODO: maybe return it with operator code as well
    try:
        from airflow.operators.python_operator import PythonOperator
        from airflow.operators.bash_operator import BashOperator

        if isinstance(t, PythonOperator):
            import inspect

            return inspect.getsource(t.python_callable)
        elif isinstance(t, BashOperator):
            return t.bash_command
    except Exception as ex:
        pass


def _get_module_code(t):
    # type: (BaseOperator) -> str
    try:
        from airflow.operators.python_operator import PythonOperator

        if isinstance(t, PythonOperator):
            import inspect

            return inspect.getsource(inspect.getmodule(t.python_callable))
    except Exception as ex:
        pass


def _get_command_from_operator(t):
    # type: (BaseOperator) -> str
    from airflow.operators.python_operator import PythonOperator
    from airflow.operators.bash_operator import BashOperator

    if isinstance(t, BashOperator):
        return "bash_command='{bash_command}'".format(bash_command=t.bash_command)
    elif isinstance(t, PythonOperator):
        return "python_callable={func}, op_kwargs={kwrags}".format(
            func=t.python_callable.__name__, kwrags=t.op_kwargs
        )


def _extract_args_from_dict(t_dict):
    # type: (Dict) -> Dict[str]
    try:
        # Return only numeric, bool and string attributes
        res = {}
        for k, v in six.iteritems(t_dict):
            if v is None or isinstance(v, TASK_ARG_TYPES):
                res[k] = v
            elif isinstance(v, list):
                res[k] = [
                    val for val in v if val is None or isinstance(val, TASK_ARG_TYPES)
                ]
            elif isinstance(v, dict):
                res[k] = _extract_args_from_dict(v)
        return res
    except Exception as ex:
        task_id = t_dict.get("task_id") or t_dict.get("_dag_id")
        logging.error("Could not collect task args for %s: %s", task_id, ex)


def _read_dag_file(dag_file):
    # TODO: Change implementation when this is done:
    # https://github.com/apache/airflow/pull/7217

    if dag_file and os.path.exists(dag_file):
        with open(dag_file) as file:
            try:
                return file.read()
            except Exception as e:
                pass

    return None


def _get_export_plugin_version():
    try:
        return pkg_resources.get_distribution("dbnd_airflow_export").version
    except Exception:
        # plugin is probably not installed but "copied" to plugins folder so we cannot know its version
        return None


### Views ###


class ExportDataViewAppBuilder(flask_appbuilder.BaseView):
    endpoint = "data_export_plugin"
    default_view = "export_data"

    @flask_appbuilder.has_access
    @flask_appbuilder.expose("/export_data")
    def export_data(self):
        from airflow.www_rbac.views import dagbag

        return export_data_api(dagbag)


class ExportDataViewAdmin(flask_admin.BaseView):
    def __init__(self, *args, **kwargs):
        super(ExportDataViewAdmin, self).__init__(*args, **kwargs)
        self.endpoint = "data_export_plugin"

    @flask_admin.expose("/")
    @flask_admin.expose("/export_data")
    def export_data(self):
        from airflow.www.views import dagbag

        return export_data_api(dagbag)


@provide_session
def _handle_export_data(
    dagbag,
    since,
    include_logs,
    include_task_args,
    include_xcom,
    dag_ids=None,
    task_quantity=None,
    session=None,
):
    include_logs = bool(include_logs)
    if since:
        since = pendulum.parse(str(since).replace(" 00:00", "Z"))

    # We monkey patch `get_current` to optimize sql querying
    old_get_current_dag = DagModel.get_current
    try:
        DagModel.get_current = _get_current_dag_model
        result = do_export_data(
            dagbag=dagbag,
            since=since,
            include_logs=include_logs,
            include_xcom=include_xcom,
            include_task_args=include_task_args,
            dag_ids=dag_ids,
            task_quantity=task_quantity,
            session=session,
        )
    finally:
        DagModel.get_current = old_get_current_dag

    if result:
        result = result.as_dict()

    return result


class JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        # convert dates and numpy objects in a json serializable format
        if isinstance(obj, datetime.datetime):
            return obj.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        elif isinstance(obj, datetime.date):
            return obj.strftime("%Y-%m-%d")

        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)


def json_response(obj):
    return Response(
        response=json.dumps(obj, indent=4, cls=JsonEncoder),
        status=200,
        mimetype="application/json",
    )


def export_data_api(dagbag):
    since = flask.request.args.get("since")
    include_logs = flask.request.args.get("include_logs")
    include_task_args = flask.request.args.get("include_task_args")
    include_xcom = flask.request.args.get("include_xcom")
    dag_ids = flask.request.args.getlist("dag_ids")
    task_quantity = flask.request.args.get("tasks", type=int)
    rbac_enabled = conf.get("webserver", "rbac").lower() == "true"

    if not since and not include_logs and not dag_ids and not task_quantity:
        new_since = datetime.datetime.utcnow().replace(
            tzinfo=pendulum.timezone("UTC")
        ) - datetime.timedelta(days=1)
        redirect_url = (
            "ExportDataViewAppBuilder" if rbac_enabled else "data_export_plugin"
        )
        redirect_url += ".export_data"
        return flask.redirect(flask.url_for(redirect_url, since=new_since, code=303))

    # do_update = flask.request.args.get("do_update", "").lower() == "true"
    # verbose = flask.request.args.get("verbose", str(not do_update)).lower() == "true"

    export_data = _handle_export_data(
        dagbag=dagbag,
        since=since,
        include_logs=include_logs,
        include_task_args=include_task_args,
        include_xcom=include_xcom,
        dag_ids=dag_ids,
        task_quantity=task_quantity,
    )
    return json_response(export_data)


### Plugin ###


class DataExportAirflowPlugin(AirflowPlugin):
    name = "dbnd_airflow_export"
    admin_views = [ExportDataViewAdmin(category="Admin", name="Export Data")]
    appbuilder_views = [
        {"category": "Admin", "name": "Export Data", "view": ExportDataViewAppBuilder()}
    ]


### Direct API ###


def export_data_directly(
    sql_alchemy_conn,
    dag_folder,
    since,
    include_logs,
    include_task_args,
    include_xcom,
    dag_ids,
    task_quantity,
):
    from airflow import models, settings, conf
    from airflow.settings import STORE_SERIALIZED_DAGS
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    conf.set("core", "sql_alchemy_conn", value=sql_alchemy_conn)
    dagbag = models.DagBag(
        dag_folder if dag_folder else settings.DAGS_FOLDER,
        include_examples=True,
        store_serialized_dags=STORE_SERIALIZED_DAGS,
    )

    engine = create_engine(sql_alchemy_conn)
    session = sessionmaker(bind=engine)
    return _handle_export_data(
        dagbag=dagbag,
        since=since,
        include_logs=include_logs,
        include_task_args=include_task_args,
        include_xcom=include_xcom,
        dag_ids=dag_ids,
        task_quantity=task_quantity,
        session=session(),
    )
