import { useActions, useValues } from 'kea'
import { useState } from 'react'

import { IconPencil } from '@posthog/icons'
import { LemonButton, LemonDialog } from '@posthog/lemon-ui'

import { pluralize } from 'lib/utils/strings'

import type { ScoutNoteApi } from 'products/signals/frontend/generated/api.schemas'

import { scoutDetailLogic } from '../../../logics/scoutDetailLogic'
import {
    isDirectScoutNote,
    NOTES_FETCH_LIMIT,
    scoutNoteWriteDisabledReason,
    scoutNotesLogic,
} from '../../../logics/scoutNotesLogic'
import { linkReportIdsInNote } from '../../../utils/scoutMemoryPresentation'
import { ScoutNoteComposer } from './ScoutNoteComposer'
import { ScoutNoteRow } from './ScoutNoteRow'

/**
 * The dialog closes itself only once the note saved. A failed request leaves it open with the text
 * still in the composer, so a long note is never lost to a transient error.
 */
function LeaveNoteDialog({
    skillName,
    onSubmit,
}: {
    skillName: string
    onSubmit: (content: string) => Promise<boolean>
}): void {
    let content = ''
    let closeDialog: (() => void) | null = null
    LemonDialog.open({
        title: skillName ? 'Tell this scout something' : 'Tell every scout something',
        description: skillName
            ? 'It picks this up on its next run and folds what it takes from it into what it has learned.'
            : 'Every scout on this project picks this up on its next run.',
        content: (close) => {
            closeDialog = close
            return (
                <ScoutNoteComposer
                    onChange={(value) => {
                        content = value
                    }}
                />
            )
        },
        shouldAwaitSubmit: true,
        primaryButton: {
            children: 'Leave note',
            preventClosing: true,
            onClick: async () => {
                const trimmed = content.trim()
                if (!trimmed) {
                    return
                }
                if (await onSubmit(trimmed)) {
                    closeDialog?.()
                }
            },
        },
        secondaryButton: { children: 'Cancel' },
    })
}

/** Opens the note composer. Sits in the scout page header and in the attention banner. */
export function LeaveScoutNoteButton({
    skillName,
    size = 'small',
    type = 'secondary',
}: {
    skillName: string
    size?: 'xsmall' | 'small'
    type?: 'primary' | 'secondary' | 'tertiary'
}): JSX.Element {
    const logic = scoutNotesLogic({ skillName })
    const { savingNote } = useValues(logic)
    const disabledReason = scoutNoteWriteDisabledReason()

    return (
        <LemonButton
            type={type}
            size={size}
            icon={<IconPencil />}
            loading={savingNote}
            disabledReason={disabledReason ?? (savingNote ? 'Saving the note' : undefined)}
            onClick={() =>
                LeaveNoteDialog({
                    skillName,
                    onSubmit: async (content) => {
                        await logic.asyncActions.createNote(content)
                        return !logic.values.lastSaveFailed
                    },
                })
            }
        >
            {skillName ? 'Tell it something' : 'Tell them all something'}
        </LemonButton>
    )
}

/**
 * What the team has told this scout: notes someone typed, plus the ones the inbox forwarded when a
 * report was dismissed, discussed, or rated. Without this the loop is invisible — people teach the
 * fleet by judging its reports and never see that it landed.
 */
export function ScoutNotesPanel({ skillName }: { skillName: string }): JSX.Element {
    const logic = scoutNotesLogic({ skillName })
    const { scoutNotes, fleetWideNotes, toldCount, notesLoading, notesLoadFailed, notesCapped } = useValues(logic)
    const { deleteNote, loadNotes } = useActions(logic)
    const { reportRows } = useValues(scoutDetailLogic({ skillName }))

    // On the fleet view every note is already in `scoutNotes`.
    const otherFleetNotes = skillName ? fleetWideNotes : []
    // Reports this scout touched in the window, so a note naming one by id can name it by title.
    const reportsById = new Map(reportRows.map(({ report }) => [report.id.toLowerCase(), report]))

    return (
        <div className="flex flex-col gap-2">
            {notesLoading && toldCount === 0 ? (
                <div className="rounded border border-primary bg-surface-primary px-4 py-6 text-center text-sm text-muted">
                    Loading notes…
                </div>
            ) : notesLoadFailed && toldCount === 0 ? (
                <div className="flex flex-col items-center gap-2 rounded border border-danger bg-danger-highlight px-4 py-6 text-center text-sm text-danger">
                    <span>Couldn't load the notes.</span>
                    <LemonButton size="xsmall" type="secondary" onClick={() => loadNotes()}>
                        Try again
                    </LemonButton>
                </div>
            ) : toldCount === 0 ? (
                <div className="rounded border border-dashed border-primary bg-surface-primary px-4 py-6 text-center text-sm text-muted">
                    Nothing yet. Leave a note to steer what this scout looks at, or dismiss one of its reports with a
                    reason, and that reaches it too.
                </div>
            ) : (
                <>
                    {scoutNotes.length > 0 && (
                        <ScoutNoteList
                            notes={scoutNotes}
                            reportsById={reportsById}
                            capped={notesCapped}
                            onDelete={deleteNote}
                        />
                    )}
                    {/* A note addressed to every scout reaches this one too, but it is not about this
                        scout, so it sits under its own label rather than in the list above. */}
                    {otherFleetNotes.length > 0 && (
                        <div className="flex flex-col gap-1">
                            <span className="text-xs font-semibold text-secondary">For every scout</span>
                            <ScoutNoteList
                                notes={otherFleetNotes}
                                reportsById={reportsById}
                                capped={notesCapped}
                                onDelete={deleteNote}
                            />
                        </div>
                    )}
                </>
            )}

            <span className="text-xs text-muted">
                New notes are picked up on the next run and folded into what it has learned. Delete a note once it has
                done its job.
            </span>
        </div>
    )
}

function ScoutNoteList({
    notes,
    reportsById,
    capped,
    onDelete,
}: {
    notes: ScoutNoteApi[]
    reportsById: Map<string, { id: string; title: string | null }>
    capped: boolean
    onDelete: (noteId: string) => void
}): JSX.Element {
    const [showAll, setShowAll] = useState(false)
    const visible = showAll ? notes : notes.slice(0, 4)

    return (
        <div className="flex flex-col rounded border border-primary bg-surface-primary">
            {visible.map((note) => (
                <ScoutNoteRow
                    key={note.id}
                    note={note}
                    content={linkReportIdsInNote(note.content, reportsById, {
                        truncateUnmatched: !isDirectScoutNote(note),
                    })}
                    onDelete={() => onDelete(note.id)}
                />
            ))}
            {notes.length > visible.length && (
                <div className="border-t border-primary px-3 py-2">
                    <LemonButton size="xsmall" type="tertiary" onClick={() => setShowAll(true)}>
                        Show all {pluralize(notes.length, 'note')}
                    </LemonButton>
                </div>
            )}
            {showAll && capped && (
                <span className="border-t border-primary px-3 py-2 text-[11px] text-muted">
                    Showing the newest {NOTES_FETCH_LIMIT} notes.
                </span>
            )}
        </div>
    )
}
