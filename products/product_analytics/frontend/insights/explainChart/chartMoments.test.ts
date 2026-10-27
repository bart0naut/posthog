import type { TrendsQuery } from '~/queries/schema/schema-general'

import { findChartMoments } from './chartMoments'
import { buildEvidenceQueries, evidenceForMoment } from './momentEvidence'

const days = (count: number): string[] =>
    Array.from({ length: count }, (_, index) => new Date(Date.UTC(2026, 8, 1 + index)).toISOString().slice(0, 10))

const steady = (count: number, level: number): number[] =>
    Array.from({ length: count }, (_, index) => level + (index % 3) - 1)

const SPIKE = steady(30, 60).map((value, index) => (index === 14 ? 498 : value))
const STEP = [...steady(15, 20), ...steady(15, 60)]

describe('explain chart moments', () => {
    test.each([
        ['a one-day spike is a peak', SPIKE, ['peak', 'latest'], [14, 14]],
        ['a lasting change of level is a shift, not a peak', STEP, ['shift', 'latest'], [15, 29]],
        ['a steady chart has only its latest point', steady(30, 50), ['latest'], [29, 29]],
    ])('%s', (_, data, kinds, firstRange) => {
        const moments = findChartMoments({ data, days: days(data.length) }, { interval: 'day', incompletePoints: 0 })

        expect(moments.map((moment) => moment.kind)).toEqual(kinds)
        expect(moments[0].range).toEqual(firstRange)
    })

    it('names the referrer that carries a peak', () => {
        const [peak] = findChartMoments({ data: SPIKE, days: days(30) }, { interval: 'day', incompletePoints: 0 })
        const query = { kind: 'TrendsQuery', series: [] } as unknown as TrendsQuery
        const rows = [
            { breakdown_value: 'news.example.com', data: SPIKE.map((_, index) => (index === 14 ? 420 : 2)) },
            { breakdown_value: '$direct', data: SPIKE.map((value, index) => (index === 14 ? 78 : value - 2)) },
        ]

        const evidence = evidenceForMoment(
            peak,
            { breakdowns: [{ property: { key: '$referring_domain', noun: 'referrer' }, query, rows }], people: null },
            { values: SPIKE, completePoints: SPIKE.length, interval: 'day', formatValue: String }
        )

        expect(evidence).toEqual([
            expect.objectContaining({
                text: expect.stringContaining('news.example.com'),
                driverShare: expect.any(Number),
            }),
        ])
        expect(evidence[0].driverShare).toBeGreaterThan(0.8)
    })

    test.each([
        ['total count', true, undefined],
        ['sum', true, 'sum'],
        ['unique users', false, 'dau'],
        ['average', false, 'avg'],
    ])('a %s chart is split to find a cause: %s', (_, splits, math) => {
        const source = {
            kind: 'TrendsQuery',
            series: [{ kind: 'EventsNode', event: 'signed_up', math, math_property: math ? 'amount' : undefined }],
        } as unknown as TrendsQuery

        expect(!!buildEvidenceQueries(source, 0)).toBe(splits)
    })
})
