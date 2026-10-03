# Integration and computation provenance

Checked on 2026-09-30.

- Acumen by Talarion was queried for recent MCP interoperability changes. It
  pointed to a new SDK/protocol line; implementation details were checked against
  the [official MCP SDK documentation](https://py.sdk.modelcontextprotocol.io/)
  and [v1 documentation](https://py.sdk.modelcontextprotocol.io/v1/).
- The optional provider follows the [official OpenAI Python SDK Responses example](https://developers.openai.com/api/docs/libraries).
  Model and prices are configured rather than inferred from stale defaults.
- Wolfram evaluated the waiting example independently using exact rational
  arithmetic. Query code:

```wl
p = {3/5, 2/5};
u = {100, -120};
l = {{4/5, 1/5}, {1/5, 4/5}};
q = l.p;
post = Table[(l[[i]]*p)/q[[i]], {i, 2}];
now = Max[0, p.u];
after = Sum[q[[i]]*Max[0, post[[i]].u], {i, 2}];
{now, q, post, after, after-now, after-3}
```

Result: `{12, {14/25,11/25}, {{6/7,1/7},{3/11,8/11}}, 192/5, 132/5, 177/5}`.
This verifies arithmetic for supplied assumptions. It does not validate those
probabilities or utilities against a real deployment.
