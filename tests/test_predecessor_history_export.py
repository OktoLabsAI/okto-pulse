"""BASE T45: consume real predecessor export without resurrecting authority."""
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.services.entity_export_renderer import render_entity_export_html, render_entity_export_markdown
from okto_pulse.core.domain.entity_export import EntityExportType
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from test_retirement_v034_source import restore_source, FIXTURES, source_cells


@pytest.mark.skipif(not os.environ.get('PULSE_PREDECESSOR_PYTHON'), reason='requires frozen installed predecessor pair')
def test_predecessor_export_retains_evidence_as_passive_history(tmp_path):
    database = restore_source(tmp_path)
    output = tmp_path / 'predecessor-export.json'
    environment = {k: v for k, v in os.environ.items() if k.upper() != 'PYTHONPATH'}
    environment['PULSE_PREDECESSOR_FIXTURE'] = str(FIXTURES / 'f2_v034_source.json')
    result = subprocess.run([environment['PULSE_PREDECESSOR_PYTHON'],
        str(Path(__file__).with_name('predecessor_export_probe.py')), str(database), str(output)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['provenance']['core']['commit'] == '207072509a282e8adec1481d05aee2d54c382bde'
    assert report['provenance']['community']['commit'] == 'b6dda64f512920fa4aaaf9d50c331b1796b87e27'
    historical = report['bundle']
    assert historical['subject']['entity_type'] == 'sprint'
    before = copy.deepcopy(historical)
    cells_before = source_cells(database)
    for renderer in (render_entity_export_markdown, render_entity_export_html):
        rendered = renderer(historical)
        for value in ('BASELINE-EVIDENCE-PRESERVED', 'BASELINE-QUESTION', 'BASELINE-ANSWER', 'BASELINE-HISTORY'):
            assert value in rendered
    assert historical == before
    assert source_cells(database) == cells_before
    assert 'sprints' not in Base.metadata.tables
    assert not any(flag.startswith('sprint.') for flag in registered_permission_flags())
    with pytest.raises(ValueError):
        EntityExportType('sprint')
