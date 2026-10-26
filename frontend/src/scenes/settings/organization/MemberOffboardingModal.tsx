import { useActions, useValues } from 'kea'

import { LemonBanner, LemonButton, LemonCheckbox, LemonSkeleton, LemonTable } from '@posthog/lemon-ui'

import { TZLabel } from 'lib/components/TZLabel'
import { LemonModal } from 'lib/lemon-ui/LemonModal'
import { fullNameOrEmail } from 'lib/utils/strings'
import { membersLogic } from 'scenes/organization/membersLogic'
import { userLogic } from 'scenes/userLogic'

import { memberOffboardingLogic } from './memberOffboardingLogic'

export function MemberOffboardingModal(): JSX.Element {
    const { member, resources, resourcesLoading, loadError, keep, removedLoading } = useValues(memberOffboardingLogic)
    const { close, loadResources, setKeep, disableAll, remove } = useActions(memberOffboardingLogic)
    const { scopedApiKeys, scopedApiKeysLoading } = useValues(membersLogic)
    const { user } = useValues(userLogic)
    const isSelf = member?.user.uuid === user?.uuid
    const hasResources = !resourcesLoading && !loadError && !!resources?.length

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
                    <LemonButton
                        type="primary"
                        status="danger"
                        loading={removedLoading}
                        disabledReason={
                            resourcesLoading
                                ? 'Checking public dashboards…'
                                : loadError || resources === null
                                  ? 'Load the dashboard review before continuing'
                                  : undefined
                        }
                        onClick={remove}
                        data-attr="confirm-org-member-offboarding"
                    >
                        {isSelf ? 'Leave' : 'Remove'}
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
                    Couldn't load public dashboards. Retry before continuing.
                </LemonBanner>
            ) : hasResources ? (
                <section className="space-y-4">
                    <h3>Publicly shared dashboards</h3>
                    <p>
                        These dashboards were created or shared by this member. Choose which public links to disable.
                        Links you keep will still work after they leave.
                    </p>
                    {!isSelf && (
                        <LemonCheckbox
                            checked={
                                keep.length === 0 ? true : keep.length === resources!.length ? false : 'indeterminate'
                            }
                            onChange={() => disableAll(keep.length > 0)}
                            disabled={removedLoading}
                            label="Disable public sharing for all dashboards"
                            data-attr="offboarding-disable-all-dashboards"
                        />
                    )}
                    <LemonTable
                        dataSource={resources ?? []}
                        rowKey="id"
                        pagination={resources!.length > 5 ? { pageSize: 5 } : undefined}
                        columns={[
                            {
                                title: 'Dashboard',
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
                                          title: 'Public sharing',
                                          key: 'disable',
                                          width: 144,
                                          render: (_: unknown, resource: NonNullable<typeof resources>[number]) => (
                                              <LemonCheckbox
                                                  checked={!keep.includes(resource.id)}
                                                  onChange={(disable) => setKeep(resource.id, !disable)}
                                                  disabled={removedLoading}
                                                  label="Disable"
                                                  data-attr="offboarding-disable-dashboard"
                                              />
                                          ),
                                      },
                                  ]
                                : []),
                        ]}
                    />
                    {isSelf && <p>Public sharing will be disabled for these dashboards when you leave.</p>}
                </section>
            ) : null}
            {!scopedApiKeysLoading && !!scopedApiKeys?.keys.length && (
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
