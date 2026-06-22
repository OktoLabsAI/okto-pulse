import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ValidationErrorDisplay, parseValidationErrorMessage } from '../ValidationErrorDisplay';

describe('ValidationErrorDisplay', () => {
  it('renders structured Resource Gate uncovered resources', () => {
    const error = JSON.stringify({
      error: 'resource_gate_spec_task_coverage',
      message: 'Cannot advance spec: mandatory spec resources are not covered by non-cancelled task cards.',
      details: {
        uncovered_resources: [
          {
            resource_type: 'architecture',
            resource_id: 'arch-1',
            resource_title: 'Resource Gate architecture',
            source_entity_type: 'spec',
            source_entity_title: 'Resource Gate spec',
            reason: 'uncovered',
            remediation: 'Attach or copy this resource directly to at least one non-cancelled task.',
          },
        ],
      },
    });

    render(<ValidationErrorDisplay error={error} />);

    expect(screen.getByText('Resource Coverage')).toBeInTheDocument();
    expect(screen.getByText(/Resource Gate architecture/)).toBeInTheDocument();
    expect(screen.getByText('Uncovered')).toBeInTheDocument();
    expect(screen.getByText(/Attach or copy this resource directly/)).toBeInTheDocument();
  });

  it('keeps legacy REQUIRED ACTION parsing', () => {
    const parsed = parseValidationErrorMessage(
      'Cannot validate spec: 1 test scenario is uncovered. REQUIRED ACTION: Link a test task.',
    );

    expect(parsed.gateType).toBe('Test Coverage');
    expect(parsed.issue).toContain('uncovered');
    expect(parsed.action).toBe('Link a test task.');
    expect(parsed.structured).toBe(false);
  });

  it('consumes the structured spec_validation gate contract (R4)', () => {
    const error = JSON.stringify({
      error: 'spec_validation_gate_required',
      code: 'spec_validation_gate_required',
      message: 'Spec Validation Gate is enabled on this board.',
      details: {
        gate_type: 'spec_validation',
        blocked_transition: 'approved->validated',
        required_status: 'validated',
        required_tool: 'okto_pulse_submit_spec_validation',
        follow_up_tool: 'okto_pulse_move_spec',
        operator_action: 'Submit a spec validation to run the semantic quality gate.',
        enforcement_mode: 'enforced',
        enforcement_active: true,
      },
    });

    const parsed = parseValidationErrorMessage(error);
    expect(parsed.structured).toBe(true);
    expect(parsed.gateType).toBe('Spec Validation');
    expect(parsed.requiredTool).toBe('okto_pulse_submit_spec_validation');
    expect(parsed.blockedTransition).toBe('approved->validated');
    expect(parsed.enforcementMode).toBe('enforced');

    render(<ValidationErrorDisplay error={error} />);
    expect(screen.getByText('Spec Validation')).toBeInTheDocument();
    expect(screen.getByText('okto_pulse_submit_spec_validation')).toBeInTheDocument();
    expect(screen.getByText('enforced')).toBeInTheDocument();
    expect(screen.getByText(/Submit a spec validation/)).toBeInTheDocument();
  });

  it('renders a non-spec gate WITHOUT the fixed "spec validated" copy (AC6)', () => {
    const error = JSON.stringify({
      error: 'test_card_completion_blocked',
      code: 'test_card_completion_blocked',
      message: 'Cannot complete this test card: 1 linked scenario still draft.',
      details: {
        gate_type: 'test_card_completion',
        required_tool: 'okto_pulse_update_test_scenario_status',
        follow_up_tool: 'okto_pulse_move_card',
        operator_action: 'Move the pending scenario to passed, then move the card to done.',
        would_block_done: true,
      },
    });

    const parsed = parseValidationErrorMessage(error);
    expect(parsed.gateType).toBe('Test Card Completion');
    expect(parsed.wouldBlockDone).toBe(true);

    render(<ValidationErrorDisplay error={error} />);
    expect(screen.getByText('Test Card Completion')).toBeInTheDocument();
    expect(screen.getByText('okto_pulse_update_test_scenario_status')).toBeInTheDocument();
    expect(screen.getByText('Blocks done')).toBeInTheDocument();
    // AC6: no generic spec-validation copy for a non-spec-validation gate.
    expect(screen.queryByText(/spec can be validated/)).toBeNull();
  });

  it('accepts required_action alias and derives advisory from enforcement_active', () => {
    const error = JSON.stringify({
      error: 'spec_qualitative_evaluation_pending',
      message: 'A qualitative evaluation is required.',
      details: {
        gate_type: 'spec_qualitative_evaluation',
        required_action: 'Submit a qualitative evaluation.',
        enforcement_active: false,
      },
    });

    const parsed = parseValidationErrorMessage(error);
    // required_action is accepted as the structured action alias.
    expect(parsed.action).toBe('Submit a qualitative evaluation.');
    // enforcement label derived from the boolean even without enforcement_mode.
    expect(parsed.enforcementMode).toBe('advisory');
    expect(parsed.enforcementActive).toBe(false);

    render(<ValidationErrorDisplay error={error} />);
    expect(screen.getByText('Qualitative Evaluation')).toBeInTheDocument();
    expect(screen.getByText('advisory')).toBeInTheDocument();
  });

  it('accepts gate_type flattened on detail (not nested under details)', () => {
    const error = JSON.stringify({
      error: 'cognitive_readiness_pending',
      gate_type: 'cognitive_readiness',
      message: 'Cognitive readiness is pending for this artifact.',
      required_tool: 'okto_pulse_kg_evaluate_cognitive_readiness',
      enforcement_active: true,
    });

    const parsed = parseValidationErrorMessage(error);
    expect(parsed.structured).toBe(true);
    expect(parsed.gateType).toBe('Cognitive Readiness');
    expect(parsed.requiredTool).toBe('okto_pulse_kg_evaluate_cognitive_readiness');
    expect(parsed.enforcementMode).toBe('enforced');
  });
});

// ===========================================================================
// R4-TEST4 (card 6bd43513, scenario ts_3749e52d) — the UI renders the REAL
// gate's copy and NEVER the fixed spec-validation phrase for a non-spec gate.
// Exercises structured detail (qualitative evaluation, resource gate, test-card
// completion) + legacy text, asserting gate_type / blocked_transition /
// required_tool when present and the absence of the spec-validation copy.
// ===========================================================================

// The exact spec-validation-only phrase: it must appear ONLY for spec_validation.
const SPEC_VALIDATED_COPY = /spec can be validated/i;
const GENERIC_GATE_COPY = /before this action can proceed/i;

describe('ts_3749e52d — gate copy is the real gate, not fixed spec validation', () => {
  it('POSITIVE CONTROL: spec_validation DOES show the spec-validation copy', () => {
    const error = JSON.stringify({
      error: 'spec_validation_gate_required',
      message: 'Spec Validation Gate is enabled on this board.',
      details: {
        gate_type: 'spec_validation',
        blocked_transition: 'approved->validated',
        required_status: 'validated',
        required_tool: 'okto_pulse_submit_spec_validation',
      },
    });
    render(<ValidationErrorDisplay error={error} />);
    // The conditional copy is present here (so its absence elsewhere is meaningful).
    expect(screen.getByText(SPEC_VALIDATED_COPY)).toBeInTheDocument();
    expect(screen.queryByText(GENERIC_GATE_COPY)).toBeNull();
  });

  it('structured qualitative evaluation: real label + transition/tool, no spec copy', () => {
    const error = JSON.stringify({
      error: 'spec_qualitative_evaluation_required',
      message: 'A qualitative evaluation must be submitted before in_progress.',
      details: {
        gate_type: 'spec_qualitative_evaluation',
        blocked_transition: 'validated->in_progress',
        required_status: 'in_progress',
        required_tool: 'okto_pulse_submit_spec_evaluation',
        operator_action: 'Submit a qualitative evaluation to score the spec.',
        enforcement_active: true,
      },
    });

    const parsed = parseValidationErrorMessage(error);
    expect(parsed.structured).toBe(true);
    expect(parsed.gateTypeCode).toBe('spec_qualitative_evaluation');
    expect(parsed.blockedTransition).toBe('validated->in_progress');
    expect(parsed.requiredTool).toBe('okto_pulse_submit_spec_evaluation');

    render(<ValidationErrorDisplay error={error} />);
    expect(screen.getByText('Qualitative Evaluation')).toBeInTheDocument();
    // gate_type / blocked_transition / required_tool surfaced in the UI.
    expect(screen.getByText('validated->in_progress')).toBeInTheDocument();
    expect(screen.getByText('okto_pulse_submit_spec_evaluation')).toBeInTheDocument();
    expect(screen.getByText(/Submit a qualitative evaluation/)).toBeInTheDocument();
    // The generic (correct) header is shown; the spec-validation copy is NOT.
    expect(screen.getByText(GENERIC_GATE_COPY)).toBeInTheDocument();
    expect(screen.queryByText(SPEC_VALIDATED_COPY)).toBeNull();
  });

  it('structured resource gate: uncovered resources rendered, no spec copy', () => {
    const error = JSON.stringify({
      error: 'resource_gate_spec_task_coverage',
      message: 'Mandatory spec resources are not covered by non-cancelled task cards.',
      details: {
        uncovered_resources: [
          {
            resource_type: 'mockup',
            resource_id: 'mk-1',
            resource_title: 'Checkout mockup',
            source_entity_type: 'spec',
            source_entity_title: 'Checkout spec',
            reason: 'uncovered',
            remediation: 'Attach or copy this resource to a non-cancelled task.',
          },
        ],
      },
    });

    render(<ValidationErrorDisplay error={error} />);
    expect(screen.getByText('Resource Coverage')).toBeInTheDocument();
    expect(screen.getByText(/Checkout mockup/)).toBeInTheDocument();
    expect(screen.getByText(/Attach or copy this resource/)).toBeInTheDocument();
    // A resource gate is NOT a spec-validation gate — no spec-validation copy.
    expect(screen.queryByText(SPEC_VALIDATED_COPY)).toBeNull();
    expect(screen.getByText(GENERIC_GATE_COPY)).toBeInTheDocument();
  });

  it('structured test-card completion: required_tool + follow-up + transition, no spec copy', () => {
    const error = JSON.stringify({
      error: 'test_card_completion_blocked',
      message: 'Cannot complete this test card: 1 linked scenario still draft.',
      details: {
        gate_type: 'test_card_completion',
        blocked_transition: 'in_progress->done',
        required_status: 'done',
        required_tool: 'okto_pulse_update_test_scenario_status',
        follow_up_tool: 'okto_pulse_move_card',
        operator_action: 'Move the pending scenario to passed, then move the card to done.',
        would_block_done: true,
      },
    });

    render(<ValidationErrorDisplay error={error} />);
    expect(screen.getByText('Test Card Completion')).toBeInTheDocument();
    expect(screen.getByText('in_progress->done')).toBeInTheDocument();
    expect(screen.getByText('okto_pulse_update_test_scenario_status')).toBeInTheDocument();
    expect(screen.getByText('okto_pulse_move_card')).toBeInTheDocument();
    expect(screen.getByText('Blocks done')).toBeInTheDocument();
    expect(screen.queryByText(SPEC_VALIDATED_COPY)).toBeNull();
    expect(screen.getByText(GENERIC_GATE_COPY)).toBeInTheDocument();
  });

  it('legacy qualitative text: inferred gate, no spec-validation copy', () => {
    // A legacy free-text evaluation/approval message (no structured detail) must
    // NOT fall back to the spec-validation phrase.
    const parsed = parseValidationErrorMessage(
      'Cannot move spec: a qualitative evaluation/approval is required first.',
    );
    expect(parsed.structured).toBe(false);
    expect(parsed.gateType).toBe('Qualitative Validation');

    render(
      <ValidationErrorDisplay error="Cannot move spec: a qualitative evaluation/approval is required first." />,
    );
    expect(screen.getByText('Qualitative Validation')).toBeInTheDocument();
    expect(screen.queryByText(SPEC_VALIDATED_COPY)).toBeNull();
    expect(screen.getByText(GENERIC_GATE_COPY)).toBeInTheDocument();
  });

  it.each([
    ['spec_qualitative_evaluation', 'A qualitative evaluation is required.'],
    ['test_card_completion', 'Cannot complete this test card.'],
    ['cognitive_readiness', 'Cognitive readiness is pending.'],
    ['resource_gate', 'Mandatory resources are uncovered.'],
  ])('non-spec gate %s never shows the spec-validation copy', (gateType, message) => {
    const error = JSON.stringify({ error: gateType, message, details: { gate_type: gateType } });
    render(<ValidationErrorDisplay error={error} />);
    expect(screen.queryByText(SPEC_VALIDATED_COPY)).toBeNull();
    // It also never shows the obsolete pre-R4 phrasing.
    expect(screen.queryByText(/before the spec can be validated/i)).toBeNull();
  });
});
