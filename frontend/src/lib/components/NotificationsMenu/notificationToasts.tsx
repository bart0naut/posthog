import { ComponentType } from 'react'

import {
    IconBell,
    IconBug,
    IconCheckCircle,
    IconClock,
    IconComment,
    IconDatabase,
    IconFlask,
    IconFolder,
    IconNotification,
    IconPieChart,
    IconPlug,
    IconSparkles,
    IconStar,
    IconToggle,
    IconTrending,
    IconWarning,
} from '@posthog/icons'
import { lemonToast } from '@posthog/lemon-ui'

import { notificationsMenuLogic } from 'lib/components/NotificationsMenu/notificationsMenuLogic'
import { FEATURE_FLAGS } from 'lib/constants'
import { featureFlagLogic } from 'lib/logic/featureFlagLogic'

import { todayShellLogic } from '~/layout/today/todayShellLogic'
import { InAppNotification } from '~/types'

const NOTIFICATION_TYPE_ICONS: Record<string, { Icon: ComponentType<{ className?: string }>; color: string }> = {
    comment_mention: { Icon: IconComment, color: 'text-primary' },
    alert_firing: { Icon: IconWarning, color: 'text-warning' },
    approval_requested: { Icon: IconCheckCircle, color: 'text-success' },
    approval_resolved: { Icon: IconCheckCircle, color: 'text-success' },
    pipeline_failure: { Icon: IconPlug, color: 'text-danger' },
    materialization_failure: { Icon: IconDatabase, color: 'text-danger' },
    issue_assigned: { Icon: IconBug, color: 'text-primary' },
    experiment_concluded: { Icon: IconFlask, color: 'text-primary' },
    project_created: { Icon: IconFolder, color: 'text-primary' },
    usage_spike: { Icon: IconTrending, color: 'text-warning' },
    reminder: { Icon: IconClock, color: 'text-primary' },
    web_analytics_digest: { Icon: IconPieChart, color: 'text-primary' },
    achievement_unlocked: { Icon: IconStar, color: 'text-warning' },
    subscription_nudge: { Icon: IconBell, color: 'text-primary' },
    data_quality_check_failure: { Icon: IconWarning, color: 'text-danger' },
    workflow_suggestion: { Icon: IconSparkles, color: 'text-primary' },
    feature_flag_failure: { Icon: IconToggle, color: 'text-danger' },
    first_report_completed: { Icon: IconSparkles, color: 'text-success' },
}

export function getNotificationIcon(notificationType: string, className: string = 'size-5'): JSX.Element {
    const { Icon, color } = NOTIFICATION_TYPE_ICONS[notificationType] ?? {
        Icon: IconNotification,
        color: 'text-secondary',
    }
    return <Icon className={`${className} ${color} shrink-0`} />
}

async function showTodayCriticalNotificationToast(notification: InAppNotification): Promise<void> {
    const { toast } = await import('@posthog/quill')
    toast({
        type: 'warning',
        title: notification.title,
        description: notification.body || undefined,
        timeout: 0,
        action: { label: 'Open', onClick: () => todayShellLogic.actions.pickPane('notifications') },
    })
}

export function showCriticalNotificationToast(notification: InAppNotification): void {
    if (featureFlagLogic.values.featureFlags[FEATURE_FLAGS.TODAY_RAIL_NAV]) {
        void showTodayCriticalNotificationToast(notification)
        return
    }
    const icon = getNotificationIcon(notification.notification_type)
    lemonToast.info(
        <div className="flex items-start gap-2">
            {icon}
            <div className="min-w-0">
                <div className="font-semibold text-xs">{notification.title}</div>
                {notification.body && (
                    <div className="text-xs text-secondary mt-0.5 line-clamp-1">{notification.body}</div>
                )}
            </div>
        </div>,
        {
            icon: false,
            autoClose: false,
            toastId: `notification-${notification.id}`,
            button: {
                label: 'Open notifications',
                action: () => notificationsMenuLogic.actions.openToUnread(),
            },
        }
    )
}
