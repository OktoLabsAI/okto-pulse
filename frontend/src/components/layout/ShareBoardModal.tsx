/**
 * Share Board Modal - Manage board sharing with other users
 */

import React, { useState, useEffect } from 'react';
import { X, Trash2, UserPlus } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDashboardApi } from '@/services/api';
import type { BoardShare } from '@/types';

interface ShareBoardModalProps {
  isOpen: boolean;
  onClose: () => void;
  boardId: string;
  boardName: string;
}

export function ShareBoardModal({ isOpen, onClose, boardId, boardName }: ShareBoardModalProps) {
  const api = useDashboardApi();
  const [shares, setShares] = useState<BoardShare[]>([]);
  const [userId, setUserId] = useState('');
  const [permission, setPermission] = useState<'viewer' | 'editor' | 'admin'>('viewer');
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (isOpen && boardId) {
      loadShares();
    }
  }, [isOpen, boardId]);

  const loadShares = async () => {
    try {
      const data = await api.listBoardShares(boardId);
      setShares(data);
    } catch {
      toast.error('Failed to load shares');
    }
  };

  const handleShare = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!userId.trim()) {
      toast.error('User ID is required');
      return;
    }

    setIsLoading(true);
    try {
      await api.shareBoard(boardId, { user_id: userId.trim(), permission });
      toast.success('Board shared!');
      setUserId('');
      await loadShares();
    } catch {
      toast.error('Failed to share board');
    } finally {
      setIsLoading(false);
    }
  };

  const handleUpdatePermission = async (shareId: string, newPermission: 'viewer' | 'editor' | 'admin') => {
    try {
      await api.updateBoardShare(boardId, shareId, { permission: newPermission });
      toast.success('Permission updated');
      await loadShares();
    } catch {
      toast.error('Failed to update permission');
    }
  };

  const handleRevoke = async (shareId: string) => {
    if (!confirm('Revoke access for this user?')) return;
    try {
      await api.revokeBoardShare(boardId, shareId);
      toast.success('Access revoked');
      await loadShares();
    } catch {
      toast.error('Failed to revoke access');
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/50" onClick={onClose} />
      <div className="relative bg-white dark:bg-gray-800 rounded-xl shadow-xl w-full max-w-md mx-4 max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            Share "{boardName}"
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <X size={20} />
          </button>
        </div>

        {/* Add share form */}
        <form onSubmit={handleShare} className="p-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex gap-2">
            <input
              type="text"
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              placeholder="User ID"
              className="flex-1 px-3 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-blue-500 outline-none"
            />
            <select
              value={permission}
              onChange={(e) => setPermission(e.target.value as 'viewer' | 'editor' | 'admin')}
              className="px-2 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="viewer">Viewer</option>
              <option value="editor">Editor</option>
              <option value="admin">Admin</option>
            </select>
            <button
              type="submit"
              disabled={isLoading}
              className="px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1 text-sm"
            >
              <UserPlus size={14} />
            </button>
          </div>
        </form>

        {/* Share list */}
        <div className="flex-1 overflow-y-auto p-4">
          {shares.length === 0 ? (
            <p className="text-center text-gray-400 dark:text-gray-500 text-sm py-4">
              Not shared with anyone yet
            </p>
          ) : (
            <ul className="space-y-2">
              {shares.map((share) => (
                <li
                  key={share.id}
                  className="flex items-center justify-between p-2 rounded-lg bg-gray-50 dark:bg-gray-700/50"
                >
                  <div>
                    <div className="text-sm font-medium text-gray-900 dark:text-white">
                      {share.user_id}
                    </div>
                    <div className="text-xs text-gray-400">
                      Shared by {share.shared_by}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <select
                      value={share.permission}
                      onChange={(e) => handleUpdatePermission(share.id, e.target.value as 'viewer' | 'editor' | 'admin')}
                      className="text-xs px-2 py-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                    >
                      <option value="viewer">Viewer</option>
                      <option value="editor">Editor</option>
                      <option value="admin">Admin</option>
                    </select>
                    <button
                      onClick={() => handleRevoke(share.id)}
                      className="p-1 text-red-400 hover:text-red-600"
                      title="Revoke access"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
