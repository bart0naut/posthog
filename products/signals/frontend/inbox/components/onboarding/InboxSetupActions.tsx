import { useActions, useValues } from 'kea'

import { IconArrowRight } from '@posthog/icons'
import { LemonButton } from '@posthog/lemon-ui'

import { inboxSetupLogic } from '../../logics/inboxSetupLogic'
import { InboxQuickSetupAction } from './InboxQuickSetupAction'

export function InboxSetupActions(): JSX.Element {
    const { continueFromWelcome } = useActions(inboxSetupLogic)
    const { saving } = useValues(inboxSetupLogic)

    return (
        <div className="flex w-full min-w-0 flex-col gap-4">
            <InboxQuickSetupAction />
            <div data-attr="inbox-welcome-set-up-manually" className="self-start">
                <LemonButton
                    type="secondary"
                    sideIcon={<IconArrowRight />}
                    onClick={continueFromWelcome}
                    disabled={saving}
                    // pinned: data-attr - the welcome funnel and test selectors match on this string
                    data-attr="inbox-setup-welcome-continue"
                >
                    Start manually
                </LemonButton>
            </div>
        </div>
    )
}
