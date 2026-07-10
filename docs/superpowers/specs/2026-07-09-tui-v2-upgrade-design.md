# TUI Charm Ecosystem Upgrade — Design Spec

## Overview

Upgrade the KubeWise terminal UI from Bubble Tea v1 / Lipgloss v1 / Bubbles v0.20
to Bubble Tea v2 / Lipgloss v2 / Bubbles v2, then migrate custom list views to
`bubbles/table` v2.

Approved three-phase plan:
- P1: Structured keymaps — already 90% done (uiKeyMap + key.Matches + help.Model in place)
- P2: Runtime v2 upgrade + table migration ← **this spec**
- P3: blit charts (exploratory, deferred)

---

## P1: Structured Keymaps (already done)

`internal/cli/ui_keys.go` defines `uiKeyMap`, `defaultUIKeys()`, `ShortHelp()`,
`FullHelp()`. All main key handlers use `key.Matches()`. Remaining:
- `handlePaletteKey` uses `key.Matches()` for list navigation but raw
  `msg.String()` for input handling (2 cases) — trivial fix, folded into P2.
- `wizard/wizard.go` is a separate model, not part of this TUI model — skip.

---

## P2: Runtime v2 Upgrade

### 2.1 Breaking Changes from v1 → v2

#### Import paths

```
github.com/charmbracelet/bubbletea  →  charm.land/bubbletea/v2
github.com/charmbracelet/lipgloss   →  charm.land/lipgloss/v2
github.com/charmbracelet/bubbles    →  charm.land/bubbles/v2
```

#### Bubble Tea v2

| v1 | v2 | Affected files |
|---|---|---|
| `tea.KeyMsg` (struct) | `tea.KeyPressMsg` (struct) | ui.go, ui_palette.go, wizard/wizard.go |
| `msg.String() == " "` (space) | `msg.String() == "space"` | ui.go, wizard/wizard.go |
| `tea.ProgramOption{}` + `tea.WithAltScreen()` | Move to `View()`: `v.AltScreen = true` | ui.go |
| `tea.WithMouseCellMotion()` | `v.MouseMode = tea.MouseModeCellMotion` | ui.go |
| `tea.WindowSizeMsg` (no change) | — | ui.go, wizard/wizard.go |
| `tea.Batch`, `tea.Tick`, `tea.Quit` | Still work | everywhere |
| `tea.Cmd` | Still works | everywhere |

#### Lipgloss v2

| v1 | v2 | Affected files |
|---|---|---|
| `lipgloss.AdaptiveColor{Light, Dark}` | `compat.AdaptiveColor{Light: lipgloss.Color(...), Dark: lipgloss.Color(...)}` | theme.go (21), wizard/styles.go (13) |
| `lipgloss.Color("#xxx")` (string type) | `lipgloss.Color()` returns `color.Color` | theme.go, wizard/styles.go |
| `lipgloss.Width()`, `lipgloss.Height()` | Still work | everywhere |

#### Bubbles v2 — Viewport

| v1 | v2 |
|---|---|
| `vp.Width = x` | `vp.SetWidth(x)` |
| `vp.Height = x` | `vp.SetHeight(x)` |
| `vp.YOffset = x` | `vp.SetYOffset(x)` |
| `vp.Width` (read) | `vp.Width()` |
| `vp.Height` (read) | `vp.Height()` |
| `vp.YOffset` (read) | `vp.YOffset()` |
| `vp.TotalLineCount()` | Still works |
| `vp.SetContent(s)` | Still works |
| `vp.GotoBottom()` | Still works |
| `vp.HighPerformanceRendering` | Removed |

#### Bubbles v2 — Textinput

| v1 | v2 |
|---|---|
| `ti.Width = w` | `ti.SetWidth(w)` |
| `textinput.DefaultKeyMap` | `textinput.DefaultKeyMap()` |
| `textinput.Blink` (Cmd) | Still `textinput.Blink` |
| `ti.Focus()` | Still works |

#### Bubbles v2 — Spinner

| v1 | v2 |
|---|---|
| `s.Spinner = spinner.Dot` | Might need `s.SetSpinner()` — check v2 API |
| `spinner.New()` | Might need `spinner.New(spinner.WithSpinner(...))` |

#### Bubbles v2 — Help

| v1 | v2 |
|---|---|
| `m.help.ShowAll = val` | `m.help.SetShowAll(val)` |
| `m.help.Width = w` | `m.help.SetWidth(w)` |

#### Bubbles v2 — List (palette)

| v1 | v2 |
|---|---|
| `list.New(...)` (constructor) | Likely functional options — check v2 API |
| `list.SetItems()` | Still works |
| `list.SetWidth()`, `list.SetHeight()` | Still works |

### 2.2 Upgrade Audit — Complete File List

| # | File | Changes Required |
|---|---|---|
| 1 | `internal/cli/ui.go` | Import paths, `tea.KeyMsg`→`KeyPressMsg`, `" "`→`"space"`, View() returns `tea.View` with `v.AltScreen`/`v.MouseMode`, `tea.ProgramOption` removal, help/viewport/spinner getter/setter, `vp.Width`→`vp.Width()` reads, `vp.Width =`→`vp.SetWidth()` |
| 2 | `internal/cli/ui_palette.go` | Import paths, `tea.KeyMsg`→`KeyPressMsg`, `ti.Width = w`→`ti.SetWidth(w)`, `textinput.DefaultKeyMap`→`DefaultKeyMap()`, raw string handling fixed |
| 3 | `internal/cli/theme.go` | `lipgloss.AdaptiveColor`→`compat.AdaptiveColor` (21 occurrences), `lipgloss.Color()` function change |
| 4 | `internal/cli/ui_keys.go` | Import paths only (no structural change) |
| 5 | `internal/cli/ui_detail.go` | Import paths, `vp.Width`→`vp.Width()`, `vp.YOffset`→`vp.YOffset()`, `vp.Height =`→`vp.SetHeight()`, `vp.YOffset =`→`vp.SetYOffset()` |
| 6 | `internal/cli/ui_plan_detail.go` | Import paths only |
| 7 | `internal/cli/events.go` | Import paths only |
| 8 | `internal/cli/install.go` | Import paths |
| 9 | `internal/cli/*_test.go` | Import paths, lipgloss API changes |
| 10 | `internal/cli/wizard/wizard.go` | Import paths, `tea.KeyMsg`→`KeyPressMsg`, `tea.Quit`, `tea.WindowSizeMsg` |
| 11 | `internal/cli/wizard/styles.go` | `lipgloss.AdaptiveColor`→`compat.AdaptiveColor` (13 occurrences) |

### 2.3 Spinner + Help v2 API Verification

Need to verify before implementation:
- `spinner.New()` constructor signature (v0.20 → v2)
- `help.New()` constructor signature
- Whether `spinner.Dot` is still valid
- Whether `textinput.Blink` is still a valid `tea.Cmd`
- `list.New()` constructor in v2 (palette uses `list.Model`)

These will be checked at implementation time via `context7_query-docs`.

### 2.4 Sequence of Changes

1. **`go.mod`**: Update bubbletea, lipgloss, bubbles to v2 in one go
2. **`internal/cli/theme.go`**: Migrate all 21 AdaptiveColor → compat (pure substitution)
3. **`internal/cli/wizard/styles.go`**: Migrate 13 AdaptiveColor (pure substitution)
4. **`internal/cli/ui.go`**: Main TUI — largest change file, requires careful audit
5. **`internal/cli/ui_detail.go`**: Viewport setter/getter migration
6. **`internal/cli/ui_palette.go`**: Textinput + KeyPressMsg
7. **`internal/cli/ui_keys.go`**: Import paths only
8. **`internal/cli/ui_plan_detail.go`**: Import paths only
9. **`internal/cli/events.go`**: Import paths only
10. **`internal/cli/install.go`**: Import paths only
11. **`internal/cli/wizard/wizard.go`**: KeyPressMsg
12. **Test files**: Import paths
13. **Build + verify**: `go build ./...`, `go vet ./...`, manual TUI test

---

## P3: Table Migration (post-v2-upgrade)

After the runtime is on v2:

1. Replace custom `headerStyle` + `col()/joinCols()` + `listHeaderStyle` + `renderList`
   pattern with `bubbles/table` v2 `table.Model`
2. Each anomaly/kpi/audit/approval/prediction list becomes a `table.Model`
3. `table.Model` handles column alignment, truncation (ansi-compatible), scrolling,
   and styling
4. Existing color/style mapping preserved via `table.Column` style funcs

---

## Risk Registers

| Risk | Mitigation |
|---|---|
| `textinput.Blink` Cmd behavior changed | Verify via context7 before implementation |
| `spinner.New()` constructor incompatibility | Check v2 constructor docs; switch to functional options if needed |
| `list.New()` palette constructor changed | Check v2 list constructor; likely same pattern with options |
| v2 bubbletea may have subtle runtime diffs | Manual terminal test after build; test keybindings, resize, scroll |
| `tea.View` returns struct not string | The compiler catches this — grep for `func.*View()` to find all implementations |
| `lipgloss.Color()` return type change | Code that uses it as string won't compile — caught by type checker |
| Incompatible transitive dependencies | `go mod tidy` may pull new deps; check for conflicts |
