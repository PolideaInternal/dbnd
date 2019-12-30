import logging

from typing import Dict, List

from dbnd import parameter
from dbnd._core.constants import SparkClusters
from dbnd._core.task.config import Config


logger = logging.getLogger(__name__)


class Qubole(object):
    aws = "aws"


class QuboleConfig(Config):
    """Databricks cloud for Apache Spark """

    _conf__task_family = "qubole"
    cluster_type = SparkClusters.qubole
    cloud = parameter(default="AWS", description="cloud")
    api_token = parameter(default="").help("API key of qubole account")[str]
    api_url = parameter(default="https://us.qubole.com/api").help(
        "API URL without version. like:'https://<ENV>.qubole.com/api'"
    )[str]
    cluster_label = parameter(default="None").help("existing cluster label")[str]
    status_polling_interval_seconds = parameter(default=10).help(
        "seconds to sleep between polling databricks for job status."
    )[int]
    show_spark_log = parameter(default=True).help(
        "if True full spark log will be printed."
    )[bool]
    qds_sdk_logging_level = parameter(default=logging.WARNING).help(
        "qubole sdk log level."
    )

    def get_spark_ctrl(self, task_run):
        from dbnd_qubole.qubole import QuboleCtrl

        return QuboleCtrl(task_run=task_run)
