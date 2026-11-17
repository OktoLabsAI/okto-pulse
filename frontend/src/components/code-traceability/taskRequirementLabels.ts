import type { TaskRequirementChoice } from '@/types/task-requirement';

export const requirementLabels: Record<TaskRequirementChoice, string> = {
  required: 'Required', optional: 'Optional', waived: 'Waived',
};
