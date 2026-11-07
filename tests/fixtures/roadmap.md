# Fixture Roadmap - Implementation Plan

A bundled plan used by the tests. It mirrors the shape of a real plan:

- an H1 document title that is not itself a milestone,
- milestones written as `##` sections,
- a nested `###` subsection whose checkboxes belong to its parent milestone,
- and a fenced code block whose `- [ ]` lines must be ignored.

## WS0 - Bootstrap

Intro text for the first milestone.

- [x] Write the root workspace files
- [x] Add the toolchain and the lockfile
- [ ] Write the first migration
- [ ] Seed the reference data

## WS1 - API

- [x] Expose the health endpoint
- [ ] Expose the milestone endpoint
- [ ] Reject unknown plan ids

### Nested details

These checkboxes belong to WS1, not to a milestone of their own.

- [ ] Document the payload shape

## WS2 - UI

Example of a plan that quotes markdown in a fence:

```markdown
- [ ] this is documentation, not a task
- [x] neither is this one
```

- [ ] Render the progress bar
- [ ] Show the per-milestone counts
- [ ] Open the detail view
- [ ] Handle the empty state
