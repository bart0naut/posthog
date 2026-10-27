import { useActions, useValues } from 'kea'
import posthog from 'posthog-js'
import type { ReactNode } from 'react'

import { IconSparkles } from '@posthog/icons'

import { LemonButton } from 'lib/lemon-ui/LemonButton'

import { Query } from '~/queries/Query/Query'
import type { InsightVizNode } from '~/queries/schema/schema-general'
import type { InsightLogicProps } from '~/types'

import {
    ExplainChartView,
    type StoryEvidenceCharts,
} from 'products/product_analytics/mcp/apps/explainChart/ExplainChartView'

import { explainChartLogic } from './explainChartLogic'

const EVIDENCE_CHARTS: StoryEvidenceCharts = {
    canShow: () => true,
    prefetch: () => {},
    // The evidence chart takes the main chart's height, so the walkthrough panel keeps its size.
    render: (query) => (
        <div data-not-quill className="flex h-(--insight-viz-min-height) flex-col">
            <Query
                key={JSON.stringify(query)}
                uniqueKey="explain-chart-evidence"
                query={{ ...(query as unknown as InsightVizNode), full: false, showHeader: false, showTable: false }}
                readOnly
                embedded
            />
        </div>
    ),
    onShow: () => posthog.capture('explain chart evidence chart shown'),
}

export function ExplainChartButton({ insightProps }: { insightProps: InsightLogicProps }): JSX.Element | null {
    const { explainAvailable, explainOpen } = useValues(explainChartLogic(insightProps))
    const { openExplain, closeExplain } = useActions(explainChartLogic(insightProps))

    if (!explainAvailable) {
        return null
    }
    return (
        <LemonButton
            size="xsmall"
            type="secondary"
            icon={<IconSparkles />}
            active={explainOpen}
            onClick={() => (explainOpen ? closeExplain() : openExplain())}
            data-attr="explain-chart-toggle"
        >
            Explain this chart
        </LemonButton>
    )
}

export interface InsightExplainChartProps {
    insightProps: InsightLogicProps
    renderChart: (overlay?: ReactNode) => ReactNode
}

/** The chart, with the walkthrough of what stands out on it beside the chart while the walkthrough is open. */
export function InsightExplainChart({ insightProps, renderChart }: InsightExplainChartProps): JSX.Element {
    const { explainOpen, explainChartData, formatValue } = useValues(explainChartLogic(insightProps))

    if (!explainOpen || !explainChartData) {
        return <>{renderChart()}</>
    }
    // The walkthrough is a quill view, so it takes quill's colors. The charts inside it keep PostHog's.
    return (
        <div data-quill>
            <ExplainChartView
                // A new query is a new story, so it starts at its summary.
                key={JSON.stringify(explainChartData.query)}
                data={explainChartData}
                renderChart={(overlay) => <div data-not-quill>{renderChart(overlay)}</div>}
                openLink={(url) => window.open(url, '_blank', 'noopener')}
                formatValue={formatValue}
                evidenceCharts={EVIDENCE_CHARTS}
                onPageChange={({ index, fromIndex, source }) =>
                    posthog.capture('explain chart page viewed', {
                        page_index: index,
                        from_page_index: fromIndex,
                        page_count: explainChartData.chapters.length + 1,
                        source,
                    })
                }
            />
        </div>
    )
}
