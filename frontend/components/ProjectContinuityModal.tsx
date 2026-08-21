import { BookOpen, Plus, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { createPromptContinuityEntryId } from '../lib/compose-project-prompt'
import type { PromptContinuity, PromptContinuityEntry } from '../types/project-model'
import { Button } from './ui/button'

export interface ProjectContinuityModalProps {
  isOpen: boolean
  onClose: () => void
  value: PromptContinuity | undefined
  onSave: (next: PromptContinuity) => void
}

function emptyContinuity(): PromptContinuity {
  return { entries: [], negativePrompt: '' }
}

const inputClassName =
  'w-full px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent'

export function ProjectContinuityModal({
  isOpen,
  onClose,
  value,
  onSave,
}: ProjectContinuityModalProps) {
  const [draft, setDraft] = useState<PromptContinuity>(emptyContinuity())

  useEffect(() => {
    if (!isOpen) return
    setDraft(structuredClone(value ?? emptyContinuity()))
  }, [isOpen, value])

  const updateEntry = (id: string, patch: Partial<Pick<PromptContinuityEntry, 'label' | 'text'>>) => {
    setDraft((prev) => ({
      ...prev,
      entries: prev.entries.map((entry) =>
        entry.id === id ? { ...entry, ...patch } : entry,
      ),
    }))
  }

  const removeEntry = (id: string) => {
    setDraft((prev) => ({
      ...prev,
      entries: prev.entries.filter((entry) => entry.id !== id),
    }))
  }

  const addEntry = () => {
    setDraft((prev) => ({
      ...prev,
      entries: [
        ...prev.entries,
        { id: createPromptContinuityEntryId(), label: '', text: '' },
      ],
    }))
  }

  const handleSave = () => {
    onSave(draft)
    onClose()
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />

      <div className="relative bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl w-full max-w-2xl mx-4">
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-800">
          <div className="flex items-center gap-2">
            <BookOpen className="h-5 w-5 text-zinc-400" />
            <h2 className="text-lg font-semibold text-white">Project continuity</h2>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="h-8 w-8 text-zinc-400 hover:text-white hover:bg-zinc-800"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>

        <div className="px-6 py-5 space-y-6 max-h-[60vh] overflow-y-auto">
          <div className="space-y-4">
            {draft.entries.map((entry) => (
              <div
                key={entry.id}
                className="rounded-lg border border-zinc-800 bg-zinc-800/40 p-4 space-y-3"
              >
                <div className="flex items-start gap-3">
                  <div className="flex-1 space-y-3">
                    <input
                      type="text"
                      value={entry.label}
                      onChange={(e) => updateEntry(entry.id, { label: e.target.value })}
                      onKeyDown={(e) => e.stopPropagation()}
                      placeholder="Label (e.g. Character bible)"
                      className={inputClassName}
                    />
                    <textarea
                      value={entry.text}
                      onChange={(e) => updateEntry(entry.id, { text: e.target.value })}
                      onKeyDown={(e) => e.stopPropagation()}
                      placeholder="Continuity text appended to every video prompt"
                      rows={4}
                      className={`${inputClassName} resize-y min-h-[96px]`}
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => removeEntry(entry.id)}
                    className="h-8 w-8 flex-shrink-0 text-zinc-400 hover:text-red-400 hover:bg-zinc-800"
                    title="Remove entry"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))}

            <Button
              type="button"
              variant="outline"
              onClick={addEntry}
              className="border-zinc-700 text-zinc-300 hover:text-white hover:bg-zinc-800"
            >
              <Plus className="h-4 w-4 mr-2" />
              Add entry
            </Button>
          </div>

          <div className="space-y-2 pt-4 border-t border-zinc-800">
            <label className="text-sm font-medium text-white">Negative prompt</label>
            <textarea
              value={draft.negativePrompt}
              onChange={(e) =>
                setDraft((prev) => ({ ...prev, negativePrompt: e.target.value }))
              }
              onKeyDown={(e) => e.stopPropagation()}
              placeholder="Project-wide negative prompt"
              rows={3}
              className={`${inputClassName} resize-y min-h-[80px]`}
            />
            <p className="text-xs text-zinc-500 leading-relaxed">
              Used for all video generations in this project. Leave empty to use the app default.
            </p>
          </div>
        </div>

        <div className="px-6 py-4 border-t border-zinc-800 flex justify-end gap-2">
          <Button
            variant="ghost"
            onClick={onClose}
            className="text-zinc-400 hover:text-white hover:bg-zinc-800"
          >
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            className="bg-blue-600 hover:bg-blue-500 text-white"
          >
            Save
          </Button>
        </div>
      </div>
    </div>
  )
}
