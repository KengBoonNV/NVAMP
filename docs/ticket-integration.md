# NVAMP ticket integration (Google Chat → support tickets)

Raise NVAMP support tickets from another system, such as a bot in existing Google Chat spaces.
A ticket created here is the same record as one raised in NVAMP's own **Raise ticket** form. It
lands in **Support tickets** as *New*, and the engineer on duty reviews it there: they open a
work order, add it to an existing one, or close it.

## Base URL and authentication

| Environment | Base URL |
|---|---|
| Dev (testing) | `https://nvamp--dev.ninjavan.apps.substrait.build` |
| Production | Production gets its own URL once NVAMP goes live, with its own key and its own data |

Every request needs the API key in a header:

```
X-API-Key: <key>
```

(`Authorization: Bearer <key>` also works.) The key is shared privately by the NVAMP owner. Keep
it in your bot's secret store, never in a Chat message or a repository. A missing or wrong key
returns `401 {"detail":"Invalid or missing API key"}`.

A `401` whose body is just `{}` comes from the platform's Google sign-in, not NVAMP. It means
`/api/integrations/` isn't yet a Public path for this environment (Substrait portal → app →
Access tab).

## Endpoints

### `POST /api/integrations/tickets` — raise a ticket

```json
{
  "machine": "SG-SG-BPS-PDS-01-SIMBA",
  "state": "Down",
  "description": "Spacer 1 belt snapped, parcels jamming at induction",
  "reporterName": "Ali Hassan",
  "reporterEmail": "ali.hassan@ninjavan.co",
  "module": "Spacer 1",
  "errorCode": "E-21",
  "contact": "+65 9123 4567",
  "sourceSpace": "PDS Ops Night Shift",
  "sourceUrl": "https://chat.google.com/room/…/…"
}
```

| Field | Required | Notes |
|---|---|---|
| `machine` | yes | Machine ID (preferred) or its NVAMP name, e.g. `Static DWS 04`. Live machines only. |
| `state` | yes | `Running` (running normally), `Degraded` (running with issues) or `Down`. Also accepts `running with issues`, `stopped`, `not running`. |
| `description` | yes | 5–4000 characters. The first line becomes the ticket title. |
| `reporterName` | yes | Shown as "Raised by …". |
| `reporterEmail` | no | Company email. Links the ticket to that person, so it appears under their tickets when they sign in to NVAMP. |
| `country` | no | `SG`, `MY`, … Use it with a machine *name* that exists in more than one country. |
| `module` | no | Module code or name, e.g. `SP1` or `Spacer 1`. Must belong to the machine. |
| `errorCode`, `contact` | no | Free text. |
| `sourceSpace`, `sourceUrl` | no | The ticket shows "via Google Chat (space) · open message". |
| `source` | no | Defaults to `gchat`. |

**201 Created**

```json
{"id":"TK-261126-WPQ2","status":"New","machineId":"SG-SG-BPS-PDS-01-SIMBA","machineName":"SIMBA bulky parcel sorter","site":"PDS","country":"SG","state":"Down","url":"https://…/#tickets"}
```

Reply in the thread with the ticket `id`.

**Errors** (each has a readable `detail` you can show in Chat as-is)

| Status | When |
|---|---|
| 400 | Bad `state`, unknown `module`, or a missing or too-short field |
| 404 | No live machine with that ID or name |
| 409 | The name matches machines in several countries. Send the ID or `country`. |
| 503 | No key is configured on this environment |

### `GET /api/integrations/machines?country=SG` — valid machines and modules

Use this to validate input, build a picker, or map the names people use in Chat to machine IDs.
It returns `{"machines":[{"id","name","type","site","country","status","modules":[{"code","name"}]}]}`.
Decommissioned machines are left out.

### `GET /api/integrations/tickets/{id}` — progress, for follow-up messages

```json
{"id":"TK-261126-WPQ2","status":"Accepted","statusLabel":"Work order opened","machineId":"…","raisedAt":"…","workOrderId":"WO-261126-AB12","workOrderStatus":"In progress","closedAt":null,"url":"…"}
```

`status` is `New` (waiting for review), `Accepted` (work order opened), `Merged` (added to an
existing work order), `Rejected` (not a machine fault) or `Closed`. Poll no more often than every
few minutes.

## Example

```bash
curl -X POST "$NVAMP/api/integrations/tickets" \
  -H "X-API-Key: $NVAMP_KEY" -H "Content-Type: application/json" \
  -d '{"machine":"SG-SG-DWS-PDS-04-SR","state":"Degraded","description":"Weight readings jumping by 2kg","reporterName":"Siti"}'
```

## Notes

- Each call creates one ticket; there's no de-duplication. If the same fault is reported twice,
  the reviewer can add the second ticket to the existing work order in NVAMP.
- Photos aren't accepted through the API. Put a link to the Chat message in `sourceUrl`; the
  reviewer can open it from the ticket.
- Every ticket raised through the API is recorded in NVAMP's Activity log.
- Key rotation: the owner can set two comma-separated keys, switch the bot to the new key, then
  remove the old one.
