# Recommended fix

A fix is available. Complete the checks needed to verify the update.

Use these steps with your coding agent. This draft contains guidance only. The findings remain open until the change is verified.

Source revision: c8ada7d5f94d59a5bd66db93efe27b96430d0204

## Affected dependencies

- @modelcontextprotocol/sdk 1\.29\.0 (npm) in packages/agent/pnpm\-lock\.yaml. Advisory-listed fixed versions: 1\.30\.1, 1\.31\.0, 1\.32\.0.
- fflate 0\.8\.2 (npm) in packages/agent/pnpm\-lock\.yaml. Advisory-listed fixed versions: 0\.8\.3.

## Advisories

- [GHSA\-22jm\-h49p\-29qw](https://osv.dev/vulnerability/GHSA-22jm-h49p-29qw)
- [GHSA\-46jr\-4wx8\-v453](https://osv.dev/vulnerability/GHSA-46jr-4wx8-v453)
- [GHSA\-6prh\-2h8m\-c8cw](https://osv.dev/vulnerability/GHSA-6prh-2h8m-c8cw)
- [GHSA\-6qxp\-vccf\-f47h](https://osv.dev/vulnerability/GHSA-6qxp-vccf-f47h)
- [GHSA\-f897\-62hf\-6gxq](https://osv.dev/vulnerability/GHSA-f897-62hf-6gxq)
- [GHSA\-px8p\-9vwx\-vf98](https://osv.dev/vulnerability/GHSA-px8p-9vwx-vf98)
- [GHSA\-rvq5\-wwqv\-78pq](https://osv.dev/vulnerability/GHSA-rvq5-wwqv-78pq)

## Steps for your coding agent

1. Review installation, build and test failures from the Security test.
2. Reproduce the failures locally and correct the update in a separate change.
3. Confirm tests pass and the advisory no longer affects the installed version.

Keep the finding open until an update or mitigation has been reviewed and verified.
