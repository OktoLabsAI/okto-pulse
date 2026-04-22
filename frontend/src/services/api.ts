/**
 * API Service - all API calls centralized
 */

import { useApiClient } from '@/contexts/ApiContext';
import type {
  Board,
  BoardSummary,
  BoardShare,
  CreateBoardRequest,
  UpdateBoardRequest,
  ShareBoardRequest,
  UpdateShareRequest,
  Card,
  CardSummary,
  CreateCardRequest,
  UpdateCardRequest,
  MoveCardRequest,
  Agent,
  AgentSummary,
  AgentBoardGrant,
  CreateAgentRequest,
  UpdateAgentRequest,
  Attachment,
  QAItem,
  CreateQARequest,
  AnswerQARequest,
  Comment,
  CreateCommentRequest,
  UpdateCommentRequest,
  CardStatus,
  Spec,
  SpecSummary,
  CreateSpecRequest,
  UpdateSpecRequest,
  MoveSpecRequest,
  SpecSkill,
  SpecKnowledge,
  SpecKnowledgeSummary,
  CreateSpecSkillRequest,
  CreateSpecKnowledgeRequest,
  SpecQAItem,
  SpecHistoryEntry,
  Ideation,
  IdeationSummary,
  IdeationStatus,
  CreateIdeationRequest,
  UpdateIdeationRequest,
  IdeationHistoryEntry,
  IdeationQAItem,
  IdeationSnapshot,
  IdeationSnapshotSummary,
  Refinement,
  RefinementSummary,
  RefinementStatus,
  CreateRefinementRequest,
  UpdateRefinementRequest,
  RefinementHistoryEntry,
  RefinementQAItem,
  RefinementSnapshot,
  RefinementSnapshotSummary,
  RefinementKnowledge,
  RefinementKnowledgeSummary,
  Guideline,
  BoardGuidelineEntry,
} from '@/types';

export function useDashboardApi() {
  const apiClient = useApiClient();

  return {
    // ==================== BOARDS ====================
    
    async createBoard(data: CreateBoardRequest): Promise<Board> {
      return apiClient.fetchJson<Board>('/boards', {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async listBoards(offset = 0, limit = 20, view: 'my' | 'shared' | 'all' = 'my'): Promise<BoardSummary[]> {
      return apiClient.fetchJson<BoardSummary[]>(`/boards?offset=${offset}&limit=${limit}&view=${view}`);
    },

    async getBoard(boardId: string): Promise<Board> {
      return apiClient.fetchJson<Board>(`/boards/${boardId}`);
    },

    async updateBoard(boardId: string, data: UpdateBoardRequest): Promise<Board> {
      return apiClient.fetchJson<Board>(`/boards/${boardId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async deleteBoard(boardId: string): Promise<void> {
      await apiClient.fetch(`/boards/${boardId}`, { method: 'DELETE' });
    },

    async getBoardColumns(boardId: string, includeArchived?: boolean): Promise<Record<CardStatus, CardSummary[]>> {
      const p = new URLSearchParams();
      if (includeArchived) p.set('include_archived', 'true');
      const qs = p.toString() ? `?${p.toString()}` : '';
      const response = await apiClient.fetchJson<{ board_id: string; columns: Record<CardStatus, CardSummary[]> }>(
        `/boards/${boardId}/columns${qs}`
      );
      return response.columns;
    },

    // ==================== SHARES ====================

    async shareBoard(boardId: string, data: ShareBoardRequest): Promise<BoardShare> {
      return apiClient.fetchJson<BoardShare>(`/boards/${boardId}/shares`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async listBoardShares(boardId: string): Promise<BoardShare[]> {
      return apiClient.fetchJson<BoardShare[]>(`/boards/${boardId}/shares`);
    },

    async updateBoardShare(boardId: string, shareId: string, data: UpdateShareRequest): Promise<BoardShare> {
      return apiClient.fetchJson<BoardShare>(`/boards/${boardId}/shares/${shareId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async revokeBoardShare(boardId: string, shareId: string): Promise<void> {
      await apiClient.fetch(`/boards/${boardId}/shares/${shareId}`, { method: 'DELETE' });
    },

    // ==================== CARDS ====================

    async createCard(boardId: string, data: CreateCardRequest): Promise<Card> {
      return apiClient.fetchJson<Card>(`/boards/${boardId}/cards`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async getCard(cardId: string): Promise<Card> {
      return apiClient.fetchJson<Card>(`/cards/${cardId}`);
    },

    async updateCard(cardId: string, data: UpdateCardRequest): Promise<Card> {
      return apiClient.fetchJson<Card>(`/cards/${cardId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async moveCard(cardId: string, data: MoveCardRequest): Promise<Card> {
      return apiClient.fetchJson<Card>(`/cards/${cardId}/move`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteCard(cardId: string): Promise<void> {
      await apiClient.fetch(`/cards/${cardId}`, { method: 'DELETE' });
    },

    async getCardActivity(cardId: string): Promise<{ id: string; action: string; actor_type: string; actor_id: string; actor_name: string; details: Record<string, unknown> | null; created_at: string }[]> {
      return apiClient.fetchJson(`/cards/${cardId}/activity`);
    },

    async getCardDependencies(cardId: string): Promise<{ id: string; title: string; status: string }[]> {
      return apiClient.fetchJson(`/cards/${cardId}/dependencies`);
    },

    async getCardDependents(cardId: string): Promise<{ id: string; title: string; status: string }[]> {
      return apiClient.fetchJson(`/cards/${cardId}/dependents`);
    },

    async addCardDependency(cardId: string, dependsOnId: string): Promise<void> {
      await apiClient.fetchJson(`/cards/${cardId}/dependencies/${dependsOnId}`, { method: 'POST' });
    },

    async removeCardDependency(cardId: string, dependsOnId: string): Promise<void> {
      await apiClient.fetch(`/cards/${cardId}/dependencies/${dependsOnId}`, { method: 'DELETE' });
    },

    async getCardSeenStatus(cardId: string): Promise<{ items: Record<string, { agent_id: string; agent_name: string; seen_at: string }[]> }> {
      return apiClient.fetchJson(`/cards/${cardId}/seen`);
    },

    async linkTestTaskToBug(cardId: string, testTaskId: string): Promise<{ success: boolean; bug_card_id: string; test_task_id: string; is_unblocked: boolean }> {
      return apiClient.fetchJson(`/cards/${cardId}/test-tasks`, {
        method: 'POST',
        body: JSON.stringify({ test_task_id: testTaskId }),
      });
    },

    async unlinkTestTaskFromBug(cardId: string, testTaskId: string): Promise<void> {
      await apiClient.fetch(`/cards/${cardId}/test-tasks/${testTaskId}`, { method: 'DELETE' });
    },

    // ==================== SPECS ====================

    async createSpec(boardId: string, data: CreateSpecRequest): Promise<Spec> {
      return apiClient.fetchJson<Spec>(`/boards/${boardId}/specs`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async listSpecs(boardId: string, status?: string, includeArchived?: boolean): Promise<SpecSummary[]> {
      const p = new URLSearchParams();
      if (status) p.set('status', status);
      if (includeArchived) p.set('include_archived', 'true');
      const qs = p.toString() ? `?${p.toString()}` : '';
      return apiClient.fetchJson<SpecSummary[]>(`/boards/${boardId}/specs${qs}`);
    },

    async getSpec(specId: string): Promise<Spec> {
      return apiClient.fetchJson<Spec>(`/specs/${specId}`);
    },

    async updateSpec(specId: string, data: UpdateSpecRequest): Promise<Spec> {
      return apiClient.fetchJson<Spec>(`/specs/${specId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async moveSpec(specId: string, data: MoveSpecRequest): Promise<Spec> {
      return apiClient.fetchJson<Spec>(`/specs/${specId}/move`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteSpec(specId: string): Promise<void> {
      await apiClient.fetch(`/specs/${specId}`, { method: 'DELETE' });
    },

    async linkCardToSpec(specId: string, cardId: string): Promise<void> {
      await apiClient.fetchJson(`/specs/${specId}/link-card/${cardId}`, { method: 'POST' });
    },

    async unlinkCardFromSpec(specId: string, cardId: string): Promise<void> {
      await apiClient.fetchJson(`/specs/${specId}/unlink-card/${cardId}`, { method: 'POST' });
    },

    // ==================== LINK TASK TO SCENARIO ====================

    async linkTaskToScenario(specId: string, scenarioId: string, cardId: string): Promise<void> {
      await apiClient.fetchJson(`/specs/${specId}/scenarios/${scenarioId}/link-task/${cardId}`, { method: 'POST' });
    },

    async unlinkTaskFromScenario(specId: string, scenarioId: string, cardId: string): Promise<void> {
      await apiClient.fetchJson(`/specs/${specId}/scenarios/${scenarioId}/unlink-task/${cardId}`, { method: 'POST' });
    },

    async linkTaskToSpecItem(specId: string, field: 'business_rules' | 'api_contracts' | 'technical_requirements' | 'decisions', itemId: string, cardId: string): Promise<Spec> {
      const spec = await this.getSpec(specId);
      const rawItems = (spec as any)[field] || [];
      // Normalize: convert legacy strings to objects (for TRs)
      const items = rawItems.map((item: any, i: number) =>
        typeof item === 'string'
          ? { id: `tr_legacy_${i}`, text: item, linked_task_ids: [] }
          : item
      );
      const updated = items.map((item: any) => {
        if (item.id === itemId) {
          const taskIds = [...(item.linked_task_ids || [])];
          if (!taskIds.includes(cardId)) taskIds.push(cardId);
          return { ...item, linked_task_ids: taskIds };
        }
        return item;
      });
      return this.updateSpec(specId, { [field]: updated } as any);
    },

    async unlinkTaskFromSpecItem(specId: string, field: 'business_rules' | 'api_contracts' | 'technical_requirements' | 'decisions', itemId: string, cardId: string): Promise<Spec> {
      const spec = await this.getSpec(specId);
      const rawItems = (spec as any)[field] || [];
      const items = rawItems.map((item: any, i: number) =>
        typeof item === 'string'
          ? { id: `tr_legacy_${i}`, text: item, linked_task_ids: [] }
          : item
      );
      const updated = items.map((item: any) => {
        if (item.id === itemId) {
          const taskIds = (item.linked_task_ids || []).filter((id: string) => id !== cardId);
          return { ...item, linked_task_ids: taskIds };
        }
        return item;
      });
      return this.updateSpec(specId, { [field]: updated } as any);
    },

    // ==================== ARCHIVE ====================

    async archiveTree(boardId: string, entityType: string, entityId: string): Promise<{ archived_count: Record<string, number> }> {
      return apiClient.fetchJson(`/boards/${boardId}/archive/${entityType}/${entityId}`, { method: 'POST' });
    },

    async restoreTree(boardId: string, entityType: string, entityId: string): Promise<{ restored_count: Record<string, number> }> {
      return apiClient.fetchJson(`/boards/${boardId}/restore/${entityType}/${entityId}`, { method: 'POST' });
    },

    // ==================== IDEATIONS ====================

    async createIdeation(boardId: string, data: CreateIdeationRequest): Promise<Ideation> {
      return apiClient.fetchJson<Ideation>(`/boards/${boardId}/ideations`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async listIdeations(boardId: string, status?: string, includeArchived?: boolean): Promise<IdeationSummary[]> {
      const p = new URLSearchParams();
      if (status) p.set('status', status);
      if (includeArchived) p.set('include_archived', 'true');
      const qs = p.toString() ? `?${p.toString()}` : '';
      return apiClient.fetchJson<IdeationSummary[]>(`/boards/${boardId}/ideations${qs}`);
    },

    async getIdeation(ideationId: string): Promise<Ideation> {
      return apiClient.fetchJson<Ideation>(`/ideations/${ideationId}`);
    },

    async updateIdeation(ideationId: string, data: UpdateIdeationRequest): Promise<Ideation> {
      return apiClient.fetchJson<Ideation>(`/ideations/${ideationId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async moveIdeation(ideationId: string, data: { status: IdeationStatus }): Promise<Ideation> {
      return apiClient.fetchJson<Ideation>(`/ideations/${ideationId}/move`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteIdeation(ideationId: string): Promise<void> {
      await apiClient.fetch(`/ideations/${ideationId}`, { method: 'DELETE' });
    },

    async evaluateIdeation(ideationId: string, data: { domains: number; domains_justification?: string; ambiguity: number; ambiguity_justification?: string; dependencies: number; dependencies_justification?: string }): Promise<Ideation> {
      return apiClient.fetchJson<Ideation>(`/ideations/${ideationId}/evaluate`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deriveSpecFromIdeation(ideationId: string): Promise<Spec> {
      return apiClient.fetchJson<Spec>(`/ideations/${ideationId}/derive-spec`, {
        method: 'POST',
      });
    },

    async listIdeationHistory(ideationId: string, limit = 50): Promise<IdeationHistoryEntry[]> {
      return apiClient.fetchJson<IdeationHistoryEntry[]>(`/ideations/${ideationId}/history?limit=${limit}`);
    },

    async listIdeationSnapshots(ideationId: string): Promise<IdeationSnapshotSummary[]> {
      return apiClient.fetchJson<IdeationSnapshotSummary[]>(`/ideations/${ideationId}/snapshots`);
    },

    async getIdeationSnapshot(ideationId: string, version: number): Promise<IdeationSnapshot> {
      return apiClient.fetchJson<IdeationSnapshot>(`/ideations/${ideationId}/snapshots/${version}`);
    },

    async listIdeationQA(ideationId: string): Promise<IdeationQAItem[]> {
      return apiClient.fetchJson<IdeationQAItem[]>(`/ideations/${ideationId}/qa`);
    },

    async createIdeationQuestion(ideationId: string, question: string): Promise<IdeationQAItem> {
      return apiClient.fetchJson<IdeationQAItem>(`/ideations/${ideationId}/qa`, {
        method: 'POST',
        body: JSON.stringify({ question }),
      });
    },

    async createIdeationChoiceQuestion(ideationId: string, data: {
      question: string;
      question_type: 'choice' | 'multi_choice';
      choices: { id: string; label: string }[];
      allow_free_text: boolean;
    }): Promise<IdeationQAItem> {
      return apiClient.fetchJson<IdeationQAItem>(`/ideations/${ideationId}/qa`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async answerIdeationQuestion(ideationId: string, qaId: string, answer: string, selected?: string[] | null): Promise<IdeationQAItem> {
      const body: Record<string, unknown> = {};
      if (answer) body.answer = answer;
      if (selected && selected.length > 0) body.selected = selected;
      return apiClient.fetchJson<IdeationQAItem>(`/ideations/${ideationId}/qa/${qaId}/answer`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },

    async deleteIdeationQuestion(ideationId: string, qaId: string): Promise<void> {
      await apiClient.fetch(`/ideations/${ideationId}/qa/${qaId}`, { method: 'DELETE' });
    },

    // ==================== REFINEMENTS ====================

    async createRefinement(ideationId: string, data: CreateRefinementRequest): Promise<Refinement> {
      return apiClient.fetchJson<Refinement>(`/ideations/${ideationId}/refinements`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async listRefinements(ideationId: string): Promise<RefinementSummary[]> {
      return apiClient.fetchJson<RefinementSummary[]>(`/ideations/${ideationId}/refinements`);
    },

    async getRefinement(refinementId: string): Promise<Refinement> {
      return apiClient.fetchJson<Refinement>(`/refinements/${refinementId}`);
    },

    async updateRefinement(refinementId: string, data: UpdateRefinementRequest): Promise<Refinement> {
      return apiClient.fetchJson<Refinement>(`/refinements/${refinementId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async moveRefinement(refinementId: string, data: { status: RefinementStatus }): Promise<Refinement> {
      return apiClient.fetchJson<Refinement>(`/refinements/${refinementId}/move`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteRefinement(refinementId: string): Promise<void> {
      await apiClient.fetch(`/refinements/${refinementId}`, { method: 'DELETE' });
    },

    async deriveSpecFromRefinement(refinementId: string): Promise<Spec> {
      return apiClient.fetchJson<Spec>(`/refinements/${refinementId}/derive-spec`, {
        method: 'POST',
      });
    },

    async listRefinementHistory(refinementId: string, limit = 50): Promise<RefinementHistoryEntry[]> {
      return apiClient.fetchJson<RefinementHistoryEntry[]>(`/refinements/${refinementId}/history?limit=${limit}`);
    },

    async listRefinementSnapshots(refinementId: string): Promise<RefinementSnapshotSummary[]> {
      return apiClient.fetchJson<RefinementSnapshotSummary[]>(`/refinements/${refinementId}/snapshots`);
    },

    async getRefinementSnapshot(refinementId: string, version: number): Promise<RefinementSnapshot> {
      return apiClient.fetchJson<RefinementSnapshot>(`/refinements/${refinementId}/snapshots/${version}`);
    },

    async listRefinementKnowledge(refinementId: string): Promise<RefinementKnowledgeSummary[]> {
      return apiClient.fetchJson<RefinementKnowledgeSummary[]>(`/refinements/${refinementId}/knowledge`);
    },

    async getRefinementKnowledge(refinementId: string, knowledgeId: string): Promise<RefinementKnowledge> {
      return apiClient.fetchJson<RefinementKnowledge>(`/refinements/${refinementId}/knowledge/${knowledgeId}`);
    },

    async createRefinementKnowledge(refinementId: string, data: { title: string; description?: string; content: string; mime_type?: string }): Promise<RefinementKnowledge> {
      return apiClient.fetchJson<RefinementKnowledge>(`/refinements/${refinementId}/knowledge`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteRefinementKnowledge(refinementId: string, knowledgeId: string): Promise<void> {
      await apiClient.fetch(`/refinements/${refinementId}/knowledge/${knowledgeId}`, { method: 'DELETE' });
    },

    async listRefinementQA(refinementId: string): Promise<RefinementQAItem[]> {
      return apiClient.fetchJson<RefinementQAItem[]>(`/refinements/${refinementId}/qa`);
    },

    async createRefinementQuestion(refinementId: string, question: string): Promise<RefinementQAItem> {
      return apiClient.fetchJson<RefinementQAItem>(`/refinements/${refinementId}/qa`, {
        method: 'POST',
        body: JSON.stringify({ question }),
      });
    },

    async createRefinementChoiceQuestion(refinementId: string, data: {
      question: string;
      question_type: 'choice' | 'multi_choice';
      choices: { id: string; label: string }[];
      allow_free_text: boolean;
    }): Promise<RefinementQAItem> {
      return apiClient.fetchJson<RefinementQAItem>(`/refinements/${refinementId}/qa`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async answerRefinementQuestion(refinementId: string, qaId: string, answer: string, selected?: string[] | null): Promise<RefinementQAItem> {
      const body: Record<string, unknown> = {};
      if (answer) body.answer = answer;
      if (selected && selected.length > 0) body.selected = selected;
      return apiClient.fetchJson<RefinementQAItem>(`/refinements/${refinementId}/qa/${qaId}/answer`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },

    async deleteRefinementQuestion(refinementId: string, qaId: string): Promise<void> {
      await apiClient.fetch(`/refinements/${refinementId}/qa/${qaId}`, { method: 'DELETE' });
    },

    // ==================== SPEC HISTORY ====================

    async listSpecHistory(specId: string, limit = 50): Promise<SpecHistoryEntry[]> {
      return apiClient.fetchJson<SpecHistoryEntry[]>(`/specs/${specId}/history?limit=${limit}`);
    },

    // ==================== SPEC Q&A ====================

    async listSpecQA(specId: string): Promise<SpecQAItem[]> {
      return apiClient.fetchJson<SpecQAItem[]>(`/specs/${specId}/qa`);
    },

    async createSpecQuestion(specId: string, question: string): Promise<SpecQAItem> {
      return apiClient.fetchJson<SpecQAItem>(`/specs/${specId}/qa`, {
        method: 'POST',
        body: JSON.stringify({ question }),
      });
    },

    async createSpecChoiceQuestion(specId: string, data: {
      question: string;
      question_type: 'choice' | 'multi_choice';
      choices: { id: string; label: string }[];
      allow_free_text: boolean;
    }): Promise<SpecQAItem> {
      return apiClient.fetchJson<SpecQAItem>(`/specs/${specId}/qa`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async answerSpecQuestion(specId: string, qaId: string, answer: string, selected?: string[] | null): Promise<SpecQAItem> {
      const body: Record<string, unknown> = {};
      if (answer) body.answer = answer;
      if (selected && selected.length > 0) body.selected = selected;
      return apiClient.fetchJson<SpecQAItem>(`/specs/${specId}/qa/${qaId}/answer`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },

    async deleteSpecQuestion(specId: string, qaId: string): Promise<void> {
      await apiClient.fetch(`/specs/${specId}/qa/${qaId}`, { method: 'DELETE' });
    },

    // ==================== SPEC SKILLS ====================

    async listSpecSkills(specId: string): Promise<SpecSkill[]> {
      return apiClient.fetchJson<SpecSkill[]>(`/specs/${specId}/skills`);
    },

    async createSpecSkill(specId: string, data: CreateSpecSkillRequest): Promise<SpecSkill> {
      return apiClient.fetchJson<SpecSkill>(`/specs/${specId}/skills`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteSpecSkill(specId: string, skillId: string): Promise<void> {
      await apiClient.fetch(`/specs/${specId}/skills/${skillId}`, { method: 'DELETE' });
    },

    // ==================== SPEC KNOWLEDGE ====================

    async listSpecKnowledge(specId: string): Promise<SpecKnowledgeSummary[]> {
      return apiClient.fetchJson<SpecKnowledgeSummary[]>(`/specs/${specId}/knowledge`);
    },

    async getSpecKnowledge(specId: string, knowledgeId: string): Promise<SpecKnowledge> {
      return apiClient.fetchJson<SpecKnowledge>(`/specs/${specId}/knowledge/${knowledgeId}`);
    },

    async createSpecKnowledge(specId: string, data: CreateSpecKnowledgeRequest): Promise<SpecKnowledge> {
      return apiClient.fetchJson<SpecKnowledge>(`/specs/${specId}/knowledge`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteSpecKnowledge(specId: string, knowledgeId: string): Promise<void> {
      await apiClient.fetch(`/specs/${specId}/knowledge/${knowledgeId}`, { method: 'DELETE' });
    },

    // ==================== AGENTS ====================

    async createAgent(data: CreateAgentRequest): Promise<Agent> {
      return apiClient.fetchJson<Agent>('/agents', {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async listMyAgents(): Promise<Agent[]> {
      return apiClient.fetchJson<Agent[]>('/agents');
    },

    async listAgentsForBoard(boardId: string): Promise<AgentSummary[]> {
      return apiClient.fetchJson<AgentSummary[]>(`/agents/board/${boardId}`);
    },

    async getAgent(agentId: string): Promise<Agent> {
      return apiClient.fetchJson<Agent>(`/agents/${agentId}`);
    },

    async updateAgent(agentId: string, data: UpdateAgentRequest): Promise<Agent> {
      return apiClient.fetchJson<Agent>(`/agents/${agentId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async regenerateAgentKey(agentId: string): Promise<{ message: string; api_key: string }> {
      return apiClient.fetchJson(`/agents/${agentId}/regenerate-key`, {
        method: 'POST',
      });
    },

    async deleteAgent(agentId: string): Promise<void> {
      await apiClient.fetch(`/agents/${agentId}`, { method: 'DELETE' });
    },

    async grantAgentBoardAccess(agentId: string, boardId: string): Promise<AgentBoardGrant> {
      return apiClient.fetchJson<AgentBoardGrant>(`/agents/${agentId}/boards/${boardId}`, {
        method: 'POST',
      });
    },

    async revokeAgentBoardAccess(agentId: string, boardId: string): Promise<void> {
      await apiClient.fetch(`/agents/${agentId}/boards/${boardId}`, { method: 'DELETE' });
    },

    async updateAgentBoardOverrides(agentId: string, boardId: string, overrides: Record<string, any> | null): Promise<any> {
      return apiClient.fetchJson(`/agents/${agentId}/boards/${boardId}`, {
        method: 'PATCH',
        body: JSON.stringify({ permission_overrides: overrides }),
      });
    },

    // ==================== ATTACHMENTS ====================

    async uploadAttachment(boardId: string, cardId: string, file: File): Promise<Attachment> {
      const formData = new FormData();
      formData.append('file', file);

      const response = await apiClient.fetch(`/attachments/${boardId}/${cardId}`, {
        method: 'POST',
        body: formData,
        headers: {}, // Let browser set Content-Type for formData
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || 'Upload failed');
      }

      return response.json();
    },

    async downloadAttachment(boardId: string, cardId: string, attachmentId: string, filename: string): Promise<void> {
      const response = await apiClient.fetch(`/attachments/${boardId}/${cardId}/${attachmentId}`);
      if (!response.ok) {
        throw new Error(`Download failed: ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },

    async deleteAttachment(boardId: string, cardId: string, attachmentId: string): Promise<void> {
      await apiClient.fetch(`/attachments/${boardId}/${cardId}/${attachmentId}`, { method: 'DELETE' });
    },

    // ==================== Q&A ====================

    async createQuestion(cardId: string, data: CreateQARequest): Promise<QAItem> {
      return apiClient.fetchJson<QAItem>(`/qa/card/${cardId}`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async answerQuestion(qaId: string, data: AnswerQARequest): Promise<QAItem> {
      return apiClient.fetchJson<QAItem>(`/qa/${qaId}/answer`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async deleteQuestion(qaId: string): Promise<void> {
      await apiClient.fetch(`/qa/${qaId}`, { method: 'DELETE' });
    },

    // ==================== COMMENTS ====================

    async createComment(cardId: string, data: CreateCommentRequest): Promise<Comment> {
      return apiClient.fetchJson<Comment>(`/comments/card/${cardId}`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async updateComment(commentId: string, data: UpdateCommentRequest): Promise<Comment> {
      return apiClient.fetchJson<Comment>(`/comments/${commentId}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async respondToChoice(commentId: string, selected: string[], freeText?: string): Promise<Comment> {
      return apiClient.fetchJson<Comment>(`/comments/${commentId}/respond`, {
        method: 'POST',
        body: JSON.stringify({ selected, free_text: freeText }),
      });
    },

    async deleteComment(commentId: string): Promise<void> {
      await apiClient.fetch(`/comments/${commentId}`, { method: 'DELETE' });
    },

    // ==================== GUIDELINES ====================

    async listGuidelines(offset = 0, limit = 50, tag?: string): Promise<Guideline[]> {
      const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      if (tag) params.set('tag', tag);
      return apiClient.fetchJson<Guideline[]>(`/guidelines?${params.toString()}`);
    },

    async createGuideline(data: { title: string; content: string; tags?: string[]; scope?: string; board_id?: string }): Promise<Guideline> {
      return apiClient.fetchJson<Guideline>('/guidelines', {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async getGuideline(id: string): Promise<Guideline> {
      return apiClient.fetchJson<Guideline>(`/guidelines/${id}`);
    },

    async updateGuideline(id: string, data: { title?: string; content?: string; tags?: string[] }): Promise<Guideline> {
      return apiClient.fetchJson<Guideline>(`/guidelines/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      });
    },

    async deleteGuideline(id: string): Promise<void> {
      await apiClient.fetch(`/guidelines/${id}`, { method: 'DELETE' });
    },

    async getBoardGuidelines(boardId: string): Promise<BoardGuidelineEntry[]> {
      return apiClient.fetchJson<BoardGuidelineEntry[]>(`/boards/${boardId}/guidelines`);
    },

    async linkGuidelineToBoard(boardId: string, guidelineId: string, priority?: number): Promise<void> {
      await apiClient.fetchJson(`/boards/${boardId}/guidelines`, {
        method: 'POST',
        body: JSON.stringify({ guideline_id: guidelineId, priority }),
      });
    },

    async createInlineGuideline(boardId: string, data: { title: string; content: string; tags?: string[]; priority?: number }): Promise<void> {
      await apiClient.fetchJson(`/boards/${boardId}/guidelines`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async unlinkGuidelineFromBoard(boardId: string, guidelineId: string): Promise<void> {
      await apiClient.fetch(`/boards/${boardId}/guidelines/${guidelineId}`, { method: 'DELETE' });
    },

    async updateGuidelinePriority(boardId: string, guidelineId: string, priority: number): Promise<void> {
      await apiClient.fetchJson(`/boards/${boardId}/guidelines/${guidelineId}`, {
        method: 'PATCH',
        body: JSON.stringify({ priority }),
      });
    },

    // ==================== ANALYTICS ====================

    async getAnalyticsOverview(from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/analytics/overview?${params.toString()}`);
    },

    async getBoardAnalyticsFunnel(boardId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/funnel?${params.toString()}`);
    },

    async getBoardAnalyticsQuality(boardId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/quality?${params.toString()}`);
    },

    async getBoardAnalyticsVelocity(boardId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/velocity?${params.toString()}`);
    },

    async getBoardAnalyticsCoverage(boardId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/coverage?${params.toString()}`);
    },

    async getBoardAnalyticsAgents(boardId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/agents?${params.toString()}`);
    },

    async getBoardAnalyticsEntities(boardId: string, type: string, from?: string, to?: string, offset?: number, limit?: number, search?: string): Promise<any> {
      const params = new URLSearchParams();
      params.set('type', type);
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      if (offset !== undefined) params.set('offset', String(offset));
      if (limit !== undefined) params.set('limit', String(limit));
      if (search) params.set('search', search);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/entities?${params.toString()}`);
    },

    async getEntityAnalytics(boardId: string, entityType: string, entityId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/entity/${entityType}/${entityId}?${params.toString()}`);
    },

    // --- Validation gate panel (spec + task gates, spec evaluation, sprint evaluation)
    async getBoardAnalyticsValidations(boardId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/validations?${params.toString()}`);
    },

    // --- Sprint analytics panel (summary + per-sprint breakdown)
    async getBoardAnalyticsSprints(boardId: string, from?: string, to?: string): Promise<any> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      return apiClient.fetchJson(`/boards/${boardId}/analytics/sprints?${params.toString()}`);
    },

    // --- Per-spec analytics detail (validation timeline, task gate summary)
    async getBoardAnalyticsSpecDetail(boardId: string, specId: string): Promise<any> {
      return apiClient.fetchJson(`/boards/${boardId}/analytics/spec/${specId}`);
    },

    // --- Per-sprint analytics detail (kanban distribution, task gate, evals)
    async getBoardAnalyticsSprintDetail(boardId: string, sprintId: string): Promise<any> {
      return apiClient.fetchJson(`/boards/${boardId}/analytics/sprint/${sprintId}`);
    },

    async exportOverviewCsv(from?: string, to?: string): Promise<void> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      const response = await apiClient.fetch(`/analytics/overview/export?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Export failed: ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `analytics-overview.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },

    async exportBoardCsv(boardId: string, from?: string, to?: string): Promise<void> {
      const params = new URLSearchParams();
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      const response = await apiClient.fetch(`/boards/${boardId}/analytics/export?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Export failed: ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `analytics-board-${boardId}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },

    async exportEntityCsv(boardId: string, entityType: string, entityId: string): Promise<void> {
      const response = await apiClient.fetch(`/boards/${boardId}/analytics/entity/${entityType}/${entityId}/export`);
      if (!response.ok) {
        throw new Error(`Export failed: ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `analytics-${entityType}-${entityId}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },
    // ==================== PERMISSION PRESETS ====================

    async listPresets(): Promise<any[]> {
      return apiClient.fetchJson<any[]>('/presets');
    },

    async getPreset(presetId: string): Promise<any> {
      return apiClient.fetchJson(`/presets/${presetId}`);
    },

    async createPreset(data: { name: string; description?: string; flags: Record<string, any> }): Promise<any> {
      return apiClient.fetchJson('/presets', { method: 'POST', body: JSON.stringify(data) });
    },

    async updatePreset(presetId: string, data: { name?: string; description?: string; flags?: Record<string, any> }): Promise<any> {
      return apiClient.fetchJson(`/presets/${presetId}`, { method: 'PUT', body: JSON.stringify(data) });
    },

    async deletePreset(presetId: string): Promise<void> {
      await apiClient.fetch(`/presets/${presetId}`, { method: 'DELETE' });
    },

    // ---- Sprints ----
    async listSprints(boardId: string, specId: string): Promise<any[]> {
      return apiClient.fetchJson(`/boards/${boardId}/specs/${specId}/sprints`);
    },

    async listBoardSprints(boardId: string, status?: string, specId?: string, includeArchived?: boolean): Promise<any[]> {
      const params = new URLSearchParams();
      if (status) params.set('status', status);
      if (specId) params.set('spec_id', specId);
      if (includeArchived) params.set('include_archived', 'true');
      const qs = params.toString();
      return apiClient.fetchJson(`/boards/${boardId}/sprints${qs ? `?${qs}` : ''}`);
    },

    async createSprint(boardId: string, specId: string, data: any): Promise<any> {
      return apiClient.fetchJson(`/boards/${boardId}/specs/${specId}/sprints`, {
        method: 'POST', body: JSON.stringify(data),
      });
    },

    async assignTasksToSprint(sprintId: string, cardIds: string[]): Promise<any> {
      return apiClient.fetchJson(`/sprints/${sprintId}/assign-tasks`, {
        method: 'POST', body: JSON.stringify({ card_ids: cardIds }),
      });
    },

    async unassignTasksFromSprint(sprintId: string, cardIds: string[]): Promise<any> {
      return apiClient.fetchJson(`/sprints/${sprintId}/unassign-tasks`, {
        method: 'POST', body: JSON.stringify({ card_ids: cardIds }),
      });
    },

    async getSprint(sprintId: string): Promise<any> {
      return apiClient.fetchJson(`/sprints/${sprintId}`);
    },

    async updateSprint(sprintId: string, data: any): Promise<any> {
      return apiClient.fetchJson(`/sprints/${sprintId}`, {
        method: 'PATCH', body: JSON.stringify(data),
      });
    },

    async moveSprint(sprintId: string, data: { status: string }): Promise<any> {
      return apiClient.fetchJson(`/sprints/${sprintId}/move`, {
        method: 'POST', body: JSON.stringify(data),
      });
    },

    async deleteSprint(sprintId: string): Promise<void> {
      await apiClient.fetch(`/sprints/${sprintId}`, { method: 'DELETE' });
    },

    async submitSprintEvaluation(sprintId: string, evaluation: any): Promise<any> {
      return apiClient.fetchJson(`/sprints/${sprintId}/evaluations`, {
        method: 'POST', body: JSON.stringify(evaluation),
      });
    },

    async listSprintHistory(sprintId: string): Promise<any[]> {
      return apiClient.fetchJson(`/sprints/${sprintId}/history`);
    },

    async suggestSprints(boardId: string, specId: string, threshold?: number): Promise<any> {
      const params = threshold ? `?threshold=${threshold}` : '';
      return apiClient.fetchJson(`/boards/${boardId}/specs/${specId}/sprints/suggest${params}`);
    },

    // ==================== TASK VALIDATION ====================

    async submitTaskValidation(cardId: string, data: any): Promise<any> {
      return apiClient.fetchJson(`/cards/${cardId}/validate`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    // ==================== SPEC VALIDATION GATE ====================

    async submitSpecValidation(specId: string, data: any): Promise<any> {
      return apiClient.fetchJson(`/specs/${specId}/validation`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },

    async listSpecValidations(specId: string): Promise<any> {
      return apiClient.fetchJson(`/specs/${specId}/validations`);
    },
  };
}
