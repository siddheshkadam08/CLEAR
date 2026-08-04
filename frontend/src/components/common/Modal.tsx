/**
 * Modal dialog.
 *
 * Full-screen sheet below `sm`, centred card above it: a 400px-wide dialog centred
 * on a phone leaves unusable margins and pushes the submit button under the
 * keyboard. Escape and backdrop both close, and body scroll is locked while open so
 * the page behind does not move under the dialog.
 */

import { X } from 'lucide-react';
import { useEffect } from 'react';
import type { ReactNode } from 'react';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children?: ReactNode;
  footer?: ReactNode;
}

export const Modal = ({ open, onClose, title, description, children, footer }: ModalProps) => {
  useEffect(() => {
    if (!open) return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center sm:p-4">
      <div
        className="absolute inset-0 bg-slate-950/50 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden
      />

      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="relative flex max-h-[92vh] w-full flex-col rounded-t-3xl bg-white dark:bg-slate-800 shadow-2xl sm:max-w-lg sm:rounded-3xl"
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 dark:border-slate-700 px-5 py-4 sm:px-6">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">{title}</h2>
            {description ? <p className="mt-1 text-sm text-slate-500">{description}</p> : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 rounded-lg p-2 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {children ? (
          <div className="flex-1 overflow-y-auto px-5 py-5 sm:px-6">{children}</div>
        ) : null}

        {footer ? (
          <div className="flex flex-col-reverse gap-2 border-t border-slate-200 dark:border-slate-700 px-5 py-4 sm:flex-row sm:justify-end sm:px-6">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
};

export default Modal;
