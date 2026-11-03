# Space shooter review — November 3, 2026

Scope: source and JavaScript logic review of the completed local-only game.
This is not a visual or hands-on browser play test. The original game was
left unchanged. The generated project took about 78 minutes and recorded
about 1.51 million local tokens, which is too costly for this simple brief.

## Findings

- **High: refresh rate changes gameplay.** `gameLoop()` calls `update()` once
  per animation frame, without elapsed time. Movement, firing, invulnerability,
  enemy spawning and particles all advance by frames. A faster display changes
  their real-world speed. Use elapsed seconds, with bounded simulation steps.
- **High: wave advancement depends on score divisibility.** An empty enemy
  list only advances the wave when `score % (50 * wave) === 0`. Missed enemies
  and varying scores can leave the wave number stuck. Track completion of a
  finite wave independently of score and the continuous spawn timer.
- **Medium: keys can stay held after focus loss.** There is no blur handler
  clearing the key map. Releasing a key outside the page may leave movement
  or firing active on return.
- **Medium: the fixed canvas and HUD can clip.** The logical canvas is
  600 × 800, the HUD is 600 pixels wide, and the body hides overflow. Add
  proportional display scaling and verify narrow and short viewports.
- **High: generated tests do not match the implementation.** `test_game.js`
  expects a 400-pixel canvas height and reads `window.player`, while the game
  uses height 800 and a private closure. These checks cannot establish that
  this version works. Saved HUD text assertions also do not prove gameplay.

The game has movement, firing, collisions, score, lives, a game-over state,
restart logic, procedural ships, stars and explosion rendering. Their presence
is a useful starting point, but does not resolve the issues above.

## Changes to Cale's verification and model selection

Interactive completion now requires observed input effects bound to the exact
HTML checked. Game completion requires two changed cases with distinct targets.
Checker failures persist in bounded SQLite records across context refreshes,
and a verified generated checker can complete without unnecessary model calls.

Model qualification separately tests tool calling, two game-logic samples, and
a complete game build. Smaller function success cannot qualify a model that
fails the full build. Only fitting local models are benchmarked, and a qualified
candidate can outrank an unqualified baseline. These fixtures measure contract
and logic correctness, not visual quality or universal game-making ability.
