import { useActions, useValues } from 'kea'

import { LemonBanner, LemonButton, LemonCheckbox, LemonSkeleton, LemonTable } from '@posthog/lemon-ui'

import { TZLabel } from 'lib/components/TZLabel'
import { LemonModal } from 'lib/lemon-ui/LemonModal'
import { fullNameOrEmail } from 'lib/utils/strings'
import { membersLogic } from 'scenes/organization/membersLogic'
import { userLogic } from 'scenes/userLogic'

import { memberOffboardingLogic } from './memberOffboardingLogic'

export function MemberOffboardingModal(): JSX.Element {
    const {
        member,
        resources,
        resourcesLoading,
        loadError,
        keep,
        step,
        dashboards,
        subscriptions,
        needsSubscriptionReview,
        removedLoading,
    } = useValues(memberOffboardingLogic)
    const { close, loadResources, setKeep, disableAll, disableAllSubscriptions, next, back, remove } =
        useActions(memberOffboardingLogic)
    const { scopedApiKeys, scopedApiKeysLoading } = useValues(membersLogic)
    const { user } = useValues(userLogic)
    const isSelf = member?.user.uuid === user?.uuid
    const hasResources = !resourcesLoading && !loadError && !!resources?.length
    const subscriptionStep = step === 'subscriptions'
    const items = subscriptionStep ? subscriptions : dashboards
    const retainedCount = items.filter((resource) => keep.includes(resource.id)).length

    return (
        <LemonModal
            isOpen={!!member}
            onClose={removedLoading ? undefined : close}
            title={
                isSelf
                    ? 'Leave organization?'
                    : `Remove ${member ? fullNameOrEmail(member.user) : 'member'} from organization?`
            }
            width={hasResources ? 720 : 576}
            footer={
                <>
                    <LemonButton
                        type="secondary"
                        onClick={close}
                        disabledReason={removedLoading ? 'Removing member…' : undefined}
                    >
                        Cancel
                    </LemonButton>
                    {subscriptionStep && (
                        <LemonButton
                            type="secondary"
                            onClick={back}
                            disabledReason={removedLoading ? 'Removing member…' : undefined}
                            data-attr="offboarding-back"
                        >
                            Back
                        </LemonButton>
                    )}
                    <LemonButton
                        type="primary"
                        status={needsSubscriptionReview ? undefined : 'danger'}
                        loading={removedLoading}
                        disabledReason={
                            resourcesLoading
                                ? 'Checking shared resources…'
                                : loadError || resources === null
                                  ? 'Load the resource review before continuing'
                                  : undefined
                        }
                        onClick={needsSubscriptionReview ? next : remove}
                        data-attr={needsSubscriptionReview ? 'offboarding-next' : 'confirm-org-member-offboarding'}
                    >
                        {needsSubscriptionReview ? 'Next: subscriptions' : isSelf ? 'Leave' : 'Remove'}
                    </LemonButton>
                </>
            }
        >
            <p>
                {isSelf
                    ? 'Are you sure you want to leave this organization? This cannot be undone. If you leave, you will no longer have access to this organization.'
                    : 'Are you sure you want to remove this member? This cannot be undone. They will no longer have access to this organization.'}
            </p>
            {resourcesLoading ? (
                <LemonSkeleton className="h-12" />
            ) : loadError ? (
                <LemonBanner type="error" action={{ children: 'Retry', onClick: loadResources }}>
                    Couldn't load shared resources. Retry before continuing.
                </LemonBanner>
            ) : hasResources ? (
                <section className="space-y-4">
                    {subscriptions.length > 0 && (
                        <div className="text-sm text-secondary">{subscriptionStep ? 'Step 2 of 2' : 'Step 1 of 2'}</div>
                    )}
                    <h3>{subscriptionStep ? 'Created subscriptions' : 'Publicly shared dashboards'}</h3>
                    {subscriptionStep ? (
                        <>
                            <p>
                                Choose which subscriptions to keep enabled. All other subscriptions will stop sending
                                reports to all recipients.
                            </p>
                            <LemonBanner type="warning">
                                This member's email address will be removed from subscription recipients, including
                                subscriptions created by other members. Subscriptions you keep can still send data to
                                other external addresses or destinations.
                            </LemonBanner>
                        </>
                    ) : (
                        <p>
                            These dashboards were created or shared by this member. Choose which public links to keep
                            enabled. All other public links will be disabled when they leave.
                        </p>
                    )}
                    {items.length === 0 ? (
                        <p>No publicly shared dashboards to review.</p>
                    ) : (
                        <>
                            {!isSelf && (
                                <LemonCheckbox
                                    checked={
                                        retainedCount === 0
                                            ? false
                                            : retainedCount === items.length
                                              ? true
                                              : 'indeterminate'
                                    }
                                    onChange={() =>
                                        (subscriptionStep ? disableAllSubscriptions : disableAll)(
                                            retainedCount === items.length
                                        )
                                    }
                                    disabled={removedLoading}
                                    label={
                                        subscriptionStep
                                            ? 'Keep all subscriptions enabled'
                                            : 'Keep public sharing enabled for all dashboards'
                                    }
                                    data-attr={
                                        subscriptionStep
                                            ? 'offboarding-disable-all-subscriptions'
                                            : 'offboarding-disable-all-dashboards'
                                    }
                                />
                            )}
                            <LemonTable
                                dataSource={items}
                                rowKey="id"
                                pagination={items.length > 5 ? { pageSize: 5 } : undefined}
                                columns={[
                                    {
                                        title: subscriptionStep ? 'Subscription' : 'Dashboard',
                                        key: 'name',
                                        render: (_, resource) => (
                                            <div className="flex flex-col min-w-0">
                                                <span className="break-words">{resource.name}</span>
                                                <span className="text-secondary text-xs break-words">
                                                    {resource.project_name}
                                                </span>
                                            </div>
                                        ),
                                    },
                                    ...(!isSelf
                                        ? [
                                              {
                                                  title: subscriptionStep ? 'Delivery' : 'Public sharing',
                                                  key: 'disable',
                                                  width: 144,
                                                  render: (_: unknown, resource: (typeof items)[number]) => (
                                                      <LemonCheckbox
                                                          checked={keep.includes(resource.id)}
                                                          onChange={(enabled) => setKeep(resource.id, enabled)}
                                                          disabled={removedLoading}
                                                          label="Keep enabled"
                                                          data-attr={
                                                              subscriptionStep
                                                                  ? 'offboarding-disable-subscription'
                                                                  : 'offboarding-disable-dashboard'
                                                          }
                                                      />
                                                  ),
                                              },
                                          ]
                                        : []),
                                ]}
                            />
                            {isSelf && (
                                <p>
                                    {subscriptionStep
                                        ? 'These subscriptions will be disabled when you leave.'
                                        : 'Public sharing will be disabled for these dashboards when you leave.'}
                                </p>
                            )}
                        </>
                    )}
                </section>
            ) : null}
            {!needsSubscriptionReview && !scopedApiKeysLoading && !!scopedApiKeys?.keys.length && (
                <div className="mt-4">
                    <LemonBanner type="warning" className="mb-2">
                        The following personal API keys will lose access to this organization and will stop working
                        immediately. Check any services that depend on them before continuing.
                    </LemonBanner>
                    <LemonTable
                        dataSource={scopedApiKeys.keys}
                        columns={[
                            { title: 'Name', dataIndex: 'name', key: 'name' },
                            {
                                title: 'Last used',
                                dataIndex: 'last_used_at',
                                key: 'last_used_at',
                                render: (lastUsed) => (lastUsed ? <TZLabel time={lastUsed} /> : 'Never'),
                            },
                        ]}
                    />
                </div>
            )}
        </LemonModal>
    )
}
