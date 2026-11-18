# SEAO disposable-database pilot

Reviewed November 11, 2026. The pilot reads local copies of official CC BY 4.0 SEAO JSON downloads. It writes a standalone SQLite schema and refuses to open an existing Contract Intelligence directory database for import. There is no dashboard, scheduled refresh or ranking integration. A read-only check of the live database found zero pilot tables and zero SEAO notices. [Données Québec catalogue and attribution](https://www.donneesquebec.ca/recherche/dataset/systeme-electronique-dappel-doffres-seao)

## Verification results

| Check | Result |
| --- | --- |
| Official source packages | [October 26–November 1](https://www.donneesquebec.ca/recherche/dataset/systeme-electronique-dappel-doffres-seao/resource/b3e87428-4678-440b-bf96-87ed8ca46bfc), [November 2–8](https://www.donneesquebec.ca/recherche/dataset/systeme-electronique-dappel-doffres-seao/resource/5d0672c8-8ac3-435d-8574-9c1fad141722). SHA-256 fingerprints begin `952f48f0` and `29ebea43` respectively. Full hashes are in the pilot audit table. |
| Identity | 6,536 tender-tagged releases across 5,996 procurement IDs. Only 4,657 distinct release IDs. The composite of procurement ID and release ID preserves releases across different notices. |
| Replay and arrival order | Replaying both files inserted zero releases and rebuilt zero notices. Separate forward- and reverse-order imports produced identical release history and tender projections. SQLite integrity check passed. |
| Amendments and date conflicts | The two files overlapped on 540 procurement IDs. The pilot left 150 records with unresolved conflicts. In 117 records, a closing date conflicted at the same source timestamp. For example, `ocds-ec9k95-20171251` had November 18 and November 25 deadlines at 2 p.m. UTC−05:00 attached to the same source timestamp. Both versions are retained and the projected deadline is withheld. A controlled later-timestamp amendment superseded the earlier deadline. |
| Sparse updates | Omitted fields preserve earlier values. The official pair did not supply a case where exactly one of two overlapping tender releases carried a closing date. A controlled out-of-order test verified that a sparse update retains the changed deadline and original notice link. |
| Cancellations | 150 projected notices were cancelled. A real example, `ocds-ec9k95-20179517`, contains a cancellation event and an active snapshot at the same timestamp. The explicit cancellation controls the status; the conflicting closing date stays withheld. |
| Time zones | Explicit source offsets are retained and normalized to UTC. Tests cover both occurrences of 1:30 a.m. during the fall clock change and reject date-only and offset-free timestamps. |
| Tests | 25 passed, including eight focused pilot tests. Awards and contract-only releases were skipped. |

The initial reverse-order run failed on a tied timestamp and rolled back the affected package. The revised pilot keeps the release evidence and records conflicting fields without choosing a deadline by release ID. It requires confirmation of publisher ordering semantics or the original SEAO notice before using a disputed record. The feed is a change history. A projected active status does not establish that the bid is currently open or suitable.

To reproduce using two official files saved locally, choose a new database path:

```powershell
py -3 -m ci.seao_pilot --db data/seao-review.sqlite3 path/to/older.json path/to/newer.json
```

## Ontario, municipal and nonprofit search

The first pass produced **zero confirmed pursuable bids** and **one verified, strong-scope lead on qualification hold**. Track the number of source-verified opportunities that clear the deadline, mandatory criteria, delivery capacity and commercial review. Keep qualification holds visible so they can be resolved without inflating that count.

- **Nonprofit, ICTC RFP 2026-10-027.** Bilingual WordPress consolidation and accessibility are aligned with the service profile. Addendum 2 extends the deadline to **November 16 at 5 p.m. ET**. The RFP states a **CAD $200,000 plus GST ceiling**. Canadian business registration, references, team capacity and liability coverage still need owner verification. Minimums include CAD $2 million general, $1 million professional and $1 million cyber coverage. Addendum 2 allows relevant partner experience when that partner will do the related work. The proposal window and delivery scope make this a stretch lead. [Buyer RFP page and addenda](https://ictc-ctic.ca/node/849)
- **Municipal, Toronto document accessibility remediation.** Discovery surfaced `Doc5821214840`. The city directs bidders to the Toronto Bids Portal and SAP Ariba. The original solicitation package and operative deadline were unavailable in this check, so the lead remains discovery-only. Ottawa's official guide also points to a mix of MERX, city listings, standing offers and select-supplier invitations for smaller procurements. [Toronto Bids Portal](https://www.toronto.ca/business-economy/doing-business-with-the-city/searching-bidding-on-city-contracts/toronto-bids-portal/) · [Ottawa bidding opportunities](https://ottawa.ca/en/business/procurement/bidding-opportunities)
- **Ontario provincial.** Supply Ontario confirms the Ontario Tenders Portal is its competitive bidding route and registered vendors can access tenders and alerts. No sanctioned bulk export was verified. Target manual checks for web, accessibility, data and workflow opportunities, and distinguish open RFPs from vendor-of-record arrangements before spending bid time. [Supply Ontario vendor guide](https://www.supplyontario.ca/become-a-vendor/)
- **Association and nonprofit discovery.** OCASI directs readers to SettlementAtWork for RFPs. Buyer-published examples from Ontario associations include member portal and WordPress work, supporting targeted buyer monitoring. Earlier examples are useful for discovering buyers. [OCASI opportunities](https://ocasi.org/careers-ocasi) · [Ontario Association of Naturopathic Doctors website RFP](https://oand.org/request-for-proposals-revamp-of-ontario-association-of-naturopathic-doctors-oand-website-member-portal/)

