"""SQL admission fence for origins retired by the verified migration journal."""

from sqlalchemy import exists, select

from okto_pulse.core.ports.work_retirement import WORK_RETIRED_ORIGIN_EVENT, WORK_RETIREMENT_FORMAT
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow


def retired_work_origin_exists(board_id, artifact_type, artifact_id):
    return exists(select(1).where(
        DomainEventRow.event_type == WORK_RETIRED_ORIGIN_EVENT,
        DomainEventRow.board_id == board_id,
        DomainEventRow.payload_json["format"].as_string() == WORK_RETIREMENT_FORMAT,
        DomainEventRow.payload_json["origin_kind"].as_string() == artifact_type,
        DomainEventRow.payload_json["origin_id"].as_string() == artifact_id,
    ))
