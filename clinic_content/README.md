# Clinic content review database

This English-language draft assumes a general outpatient clinic in Taiwan. It is **not approved public information for any particular clinic**. Neither the current website nor Lumi reads this database. Omitting the word “demo” does not make unverified clinic details factual.

## What's included

- `seed.json`: 11 proposed FAQs about registration, queue numbers, clinic information, fees, and medication questions; plus four active ingredients with general uses, possible adverse effects, and Taiwan FDA source links.
- `app/clinic_content.py`: loads the draft into a local SQLite database for review. The default database is `data/clinic_content_review_en.db`, which is excluded by `.gitignore`. Rerunning the initializer does not overwrite edited records.
- All records start with a `pending_*` status. A clinician or pharmacist must review medication content, and the clinic must confirm which medicines it actually offers or prescribes.

Create the local database:

```powershell
python -m app.clinic_content --db data/clinic_content_review_en.db
```

## Information needed before publication

1. The clinic's name, specialty, address, phone number, consultation hours, closures, and source of schedule updates.
2. Registration channels, appointment and walk-in numbering, missed-number policy, queue-system integration, and update frequency.
3. The live number being called for each room or doctor, with a last-updated time. Do not infer people ahead or waiting minutes from ticket-number differences.
4. Clinic-approved fees, services, medication ingredients, and the corresponding prescription labels or package inserts.
5. Administrative review by the clinic owner or staff, medical review by a doctor or pharmacist, and clear responsibility for future updates.

Do not add identifiable patient records, prescriptions, identity numbers, or contact details to this draft. The medication entries do not provide individualized diagnoses, doses, or dosing schedules and do not replace prescription labels or professional advice.
