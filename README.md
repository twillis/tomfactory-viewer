# tomfactory-viewer

Built by the factory, one request at a time.

## How this was built

This app was written by [tomfactory](https://github.com/twillis/tomfactory), a small
software factory that runs AI coding agents through a test-first cycle. I wrote the
requests and approved each plan; the agents planned, wrote the tests, wrote the code,
reviewed the architecture, documented and reviewed the result.

- **38 of this repository's 43 commits were made by the factory** (author `tomfactory`).
  The other five are me: the factory's settings, its database contract, and a merge.
- **17 runs**: 11 accepted, 2 rejected by the factory's own reviewer, 4 stopped for me to
  look at. About $100 of agent time in all, on a subscription.
- **Tests first, every time.** Each feature's tests were written by one agent and seen to
  fail before another agent built it, and they run in a sandbox with no network and no
  access to anything outside the checkout.
- **It went wrong in instructive ways.** By the fourth feature the tests had grown to 6,625
  lines for 634 lines of code, because the test writer was told to cover edge cases and
  never told to stop. The fix went into the factory (a budget, and a way to tidy without a
  failing test), and two tidy runs brought the tests back to about 1,800 lines with more
  cases covered.
- `docs/backlog.md` is what the factory's architect persona noted for later.

What it does: a home page, a list of every run, a page for one run with its steps, a page
for one step with its checks and events, and a live view of a run in progress. Python
standard library only.

