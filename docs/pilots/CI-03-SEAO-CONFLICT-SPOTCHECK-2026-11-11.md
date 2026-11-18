# SEAO tied-source-timestamp conflict: one primary-source spot check

**Observation dated:** November 11, 2026. **Scope:** one identified disputed OCID, not all 150 conflicts. **Method:** public read-only SEAO buyer notice and an independent secondary archive; no logged-in tender attachments, private data, downloads to production, or changes to live SQLite.

## Example: reference 20171251

| Field | Evidence |
|---|---|
| OCID | `ocds-ec9k95-20171251` |
| Buyer | Agence de mobilité durable |
| Tender / notice | `AO-AMD-26-021`, fabrication de pièces métalliques pour stationnement (goods, **not** a software-service lead) |
| Pilot conflict | Reported conflicting closing deadlines at same source timestamp: **Nov 18 and Nov 25, 2026 at 14:00 Québec standard time** |
| **Official buyer notice read Nov 11** | [SEAO original](https://seao.gouv.qc.ca/avis-resultat-recherche/consulter?ItemId=ada6f680-92fc-4c86-a61a-59a8a0f487e3): **November 25, 2026, before 14:00, Heure légale du Québec**. Status `Publié`. Original publication October 28, 2026 10:23:01. Official page shows Addenda 1–4, with Addenda 4 labelled amendment, posted November 5 at 15:03:28. |
| Independent secondary history (NOT controlling) | [Soumissio notice](https://soumissio.ca/tenders/ocds-ec9k95-20171251) has surfaced Nov 18 in some snapshots but its read-only page now displays **Nov 25**. It warns to consult original SEAO. Its metadata says original collection Nov 2 and update Nov 9. |
| Interpretation | The **current externally checked closing date** is Nov 25 14:00 Québec (UTC-5). This supports a source-authority override for a **separately verified current notice snapshot**, but does **not** establish which of two tied-timestamp SEAO releases should take precedence or that the remaining conflicting records follow the same explanation. |
| Production and eligibility implications | Existing disposable parser should retain both releases, conflict marker, and original values. A verified override should be stored as a distinct **buyer-page evidence record** with source URL, observed_at, override value, time zone, and actor; never mutate or silently erase source release history. This example is irrelevant to bidder's software scope. |

## Proposed next falsification run (not yet performed)

1. Using disposable `CI-03` SQLite and the two hashed official JSON files, produce a **stable sample of 12 or more records**: 6 tied-timestamp disputed deadlines, 3 tied conflicts in other fields/cancellations, 3 controls without conflicts. Freeze a random-independent or fixed-OCID selection method before visiting notice pages.
2. Save each record's release timestamps, source OCID/IDs, both claimed values, and exact linked SEAO notice URL from JSON. Fetch **the public notice only**, no gated document access. Record buyer-page value, visit date, amendment identifiers if displayed, whether linked notice is accessible, and mismatch class.
3. Measure fraction resolved from buyer page vs still unknown, and whether observed buyer-page field corresponds to latest published amendment, ties, or page-only updates. Do not infer all 117 can be resolved because one can.
4. Implement no live integration or automatic preferred-date selection unless the rule is documented and tested. The app may show a read-only `Buyer site currently says X; raw feed conflict unresolved` banner with time of verification, but do not call either source set complete.
5. Report conflict rate with denominators: **150/5,996 procurement IDs** projected conflicting in pilot; **117/5,996** with disputed deadline; not a random prevalence estimate across all SEAO data. Account for document/source access failures and stale snapshots.

## Sources

- [Pilot report](CI-03-PILOT.md), from repository commit `edb26a6`.
- Official [Données Québec SEAO catalogue](https://www.donneesquebec.ca/recherche/dataset/systeme-electronique-dappel-doffres-seao); reused with CC BY 4.0 attribution.
- [SEAO original notice, ItemId=ada6f680-92fc-4c86-a61a-59a8a0f487e3](https://seao.gouv.qc.ca/avis-resultat-recherche/consulter?ItemId=ada6f680-92fc-4c86-a61a-59a8a0f487e3).

**Disposition:** One official notice date verified independently of pilot; no production change. This file is research, not permission to promote SEAO into the live dashboard.
