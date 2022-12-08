import logging
import typing as t
from datetime import datetime, timedelta, timezone

from airflow.api.common.experimental.delete_dag import delete_dag
from airflow.exceptions import AirflowException, DagNotFound
from airflow.models import BaseOperator, DagRun, DagTag, XCom
from airflow.utils.session import provide_session
from airflow.utils.state import DagRunState
from sqlalchemy.orm import Session

from sqlmesh.schedulers.airflow import common
from sqlmesh.utils.date import now
from sqlmesh.utils.errors import SQLMeshError

logger = logging.getLogger(__name__)


# Used to omit Optional for session instances supplied by
# Airflow at runtime. This makes the type signature cleaner
# and prevents mypy from complaining.
PROVIDED_SESSION: Session = t.cast(Session, None)


@provide_session
def get_snapshot_dag_ids(session: Session = PROVIDED_SESSION) -> t.List[str]:
    dag_tags = (
        session.query(DagTag).filter(DagTag.name == common.SNAPSHOT_AIRFLOW_TAG).all()
    )
    return [tag.dag_id for tag in dag_tags]


@provide_session
def get_finished_plan_application_dag_ids(
    ttl: t.Optional[timedelta] = None, session: Session = PROVIDED_SESSION
) -> t.Set[str]:
    dag_ids = (
        session.query(DagTag.dag_id)
        .join(DagRun, DagTag.dag_id == DagRun.dag_id)
        .filter(
            DagTag.name == common.PLAN_AIRFLOW_TAG,
            DagRun.state.in_((DagRunState.SUCCESS, DagRunState.FAILED)),
        )
    )
    if ttl is not None:
        dag_ids = dag_ids.filter(DagRun.last_scheduling_decision <= now() - ttl)
    return {dag_id[0] for dag_id in dag_ids.all()}


@provide_session
def delete_dags(dag_ids: t.Set[str], session: Session = PROVIDED_SESSION) -> None:
    for dag_id in dag_ids:
        try:
            delete_dag(dag_id, session=session)
        except DagNotFound:
            logger.warning("DAG '%s' was not found", dag_id)
        except AirflowException:
            logger.warning("Failed to delete DAG '%s'", dag_id, exc_info=True)


@provide_session
def delete_xcoms(
    dag_id: str,
    keys: t.Set[str],
    task_id: t.Optional[str] = None,
    run_id: t.Optional[str] = None,
    session: Session = PROVIDED_SESSION,
) -> None:
    query = session.query(XCom).filter(XCom.dag_id == dag_id, XCom.key.in_(keys))
    if task_id is not None:
        query = query.filter_by(task_id=task_id)
    if run_id is not None:
        query = query.filter_by(run_id=run_id)
    query.delete(synchronize_session=False)


def discover_engine_operator(name: str) -> t.Type[BaseOperator]:
    name = name.lower()

    if name in ("spark", "spark-submit", "spark_submit"):
        try:
            from sqlmesh.schedulers.airflow.operators.spark_submit import (
                SQLMeshSparkSubmitOperator,
            )

            return SQLMeshSparkSubmitOperator
        except ImportError:
            raise SQLMeshError("Failed to automatically discover an operator for Spark")
    if name in ("databricks", "databricks-sql", "databricks_sql"):
        try:
            from sqlmesh.schedulers.airflow.operators.databricks import (
                SQLMeshDatabricksSQLOperator,
            )

            return SQLMeshDatabricksSQLOperator
        except ImportError:
            raise SQLMeshError(
                "Failed to automatically discover an operator for Databricks"
            )
    raise ValueError(f"Unsupported engine name '{name}'")


def safe_utcfromtimestamp(timestamp: t.Optional[float]) -> t.Optional[datetime]:
    return (
        datetime.utcfromtimestamp(timestamp).replace(tzinfo=timezone.utc)
        if timestamp is not None
        else None
    )
