# Migrating Miners To SAT

SAT is additive. Keep the current agent path live.

SAT uses SSH and Hermes. Validators see signed hash-only rows. Scored SAT requires operator release notice.

## Add SAT

1. Keep current miner running.
2. Pick a SAT host.
3. Create an unprivileged SSH user.
4. Install Hermes for that user.
5. Add your solver wrapper.
6. Test toy DIMACS locally.
7. Register hotkey, host, SSH user, and hardware line.
8. Join shadow rounds.
9. Move to scored rounds only after release notice.

## Wrapper Output

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

No source. No logs. No extra keys.

## Private

- solver source
- solver strategy
- raw CNFs
- raw solutions
- logs
- benchmark notes
