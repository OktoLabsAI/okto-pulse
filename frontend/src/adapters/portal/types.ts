import type { FC } from 'react';

export interface PortalAdapter {
  PortalBar: FC<{
    visible: boolean;
    onToggleVisibility: () => void;
  }> | null;
  ShareBoardModal: FC<{
    isOpen: boolean;
    onClose: () => void;
    boardId: string;
    boardName: string;
  }> | null;
}
