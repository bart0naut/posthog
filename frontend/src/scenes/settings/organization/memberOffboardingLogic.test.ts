import { MOCK_SECOND_ORGANIZATION_MEMBER } from 'lib/api.mock'

import { expectLogic } from 'kea-test-utils'

import { useMocks } from '~/mocks/jest'
import { initKeaTests } from '~/test/init'

import { memberOffboardingLogic } from './memberOffboardingLogic'

const resources = [
    {
        id: 'public_link:dashboard:1:1',
        kind: 'dashboard',
        name: 'Product overview',
        team_id: 1,
        project_name: 'Website',
    },
]

describe('memberOffboardingLogic', () => {
    let logic: ReturnType<typeof memberOffboardingLogic.build>
    let submitted: unknown[]

    beforeEach(() => {
        submitted = []
        useMocks({
            get: {
                '/api/organizations/:organization/members/:user/offboarding/': resources,
                '/api/organizations/:organization/members/:user/scoped_api_keys/': { has_keys: false, keys: [] },
            },
            post: {
                '/api/organizations/:organization/members/:user/offboard/': async ({ request }) => {
                    submitted.push(await request.json())
                    return [204, null]
                },
            },
        })
        initKeaTests()
        logic = memberOffboardingLogic()
        logic.mount()
    })

    it('defaults to disabling resources and submits the explicit keep choices', async () => {
        await expectLogic(logic, () => logic.actions.open(MOCK_SECOND_ORGANIZATION_MEMBER)).toFinishAllListeners()
        expect(logic.values.resources).toEqual(resources)
        expect(logic.values.keep).toEqual([])
        logic.actions.setKeep('public_link:dashboard:1:1', true)
        await expectLogic(logic, () => logic.actions.remove()).toDispatchActions(['removeSuccess'])
        expect(submitted).toEqual([{ keep: ['public_link:dashboard:1:1'] }])
        expect(logic.values.member).toBeNull()
    })

    it('supports keeping all dashboards and disabling all again', async () => {
        await expectLogic(logic, () => logic.actions.open(MOCK_SECOND_ORGANIZATION_MEMBER)).toFinishAllListeners()
        logic.actions.disableAll(false)
        expect(logic.values.keep).toEqual(resources.map((resource) => resource.id))
        logic.actions.disableAll(true)
        expect(logic.values.keep).toEqual([])
    })

    it('blocks removal when the resource review fails and permits retry', async () => {
        useMocks({
            get: {
                '/api/organizations/:organization/members/:user/offboarding/': () => [
                    500,
                    { detail: 'Review unavailable' },
                ],
            },
        })
        await expectLogic(logic, () => logic.actions.open(MOCK_SECOND_ORGANIZATION_MEMBER)).toDispatchActions([
            'loadResourcesFailure',
        ])
        await expectLogic(logic, () => logic.actions.remove()).toFinishAllListeners()
        expect(submitted).toEqual([])
        expect(logic.values.loadError).toBeTruthy()
        useMocks({ get: { '/api/organizations/:organization/members/:user/offboarding/': resources } })
        await expectLogic(logic, () => logic.actions.loadResources()).toDispatchActions(['loadResourcesSuccess'])
        expect(logic.values.loadError).toBeNull()
    })
})
