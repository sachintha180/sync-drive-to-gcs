# GCP Setup: sync-drive-to-gcs

End-to-end steps to provision the Google Cloud infrastructure that syncs Google Drive files to a GCS bucket on a schedule.

> All multi-line commands use `` ` `` for line continuation.

---

## 1. Enable APIs

Enable all required Google Cloud APIs upfront:

```
gcloud services enable drive.googleapis.com
gcloud services enable secretmanager.googleapis.com
gcloud services enable cloudfunctions.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable cloudscheduler.googleapis.com
```

---

## 2. Create Storage Bucket

Create the GCS bucket that Drive files will be synced into:

```
gcloud storage buckets create gs://the-cs-class `
--location=asia-south1 `
--uniform-bucket-level-access `
--public-access-prevention
```

- `--location`: region where the bucket's data is stored.
- `--uniform-bucket-level-access`: disables per-object ACLs; access is controlled at the bucket level via IAM only.
- `--public-access-prevention`: hard-blocks any public access, even if accidentally granted later.

Verify the configuration:

```
gcloud storage buckets describe gs://the-cs-class `
--format="json(iamConfiguration,publicAccessPrevention)"
```

If you ever need to copy content from one bucket to another:

```
gcloud storage cp --recursive gs://source-bucket-name/* gs://destination-bucket-name/
```

---

## 3. Create Service Account

Create the `drive-reader` service account used by the Cloud Function and Scheduler:

```
gcloud iam service-accounts create drive-reader `
--display-name="drive-reader"
```

Grant it Storage Object Admin on the bucket (list, read, write, delete):

```
gcloud storage buckets add-iam-policy-binding gs://the-cs-class `
--member="serviceAccount:drive-reader@portfolio-492410.iam.gserviceaccount.com" `
--role="roles/storage.objectAdmin"
```

- `--member`: the identity being granted access: in this case, the `drive-reader` service account.
- `--role=roles/storage.objectAdmin`: grants full object-level access (list, get, create, delete) but not bucket-level config changes.

Optionally, generate a local key for development use:

```
gcloud iam service-accounts keys create ./secrets/drive-reader-sa-key.json `
--iam-account="drive-reader@portfolio-492410.iam.gserviceaccount.com"
```

---

## 4. Generate & Store OAuth Credentials

Run the local script to generate a Google Drive OAuth refresh token:

```
python -m scripts.generate_refresh_token
```

This writes credentials to `secrets/drive-reader-oauth.json`. Store them in Secret Manager so the Cloud Function can access them at runtime:

```
gcloud secrets create drive-reader-oauth-creds `
--data-file="./secrets/drive-reader-oauth.json"
```

Grant `drive-reader` permission to read the secret at runtime:

```
gcloud secrets add-iam-policy-binding drive-reader-oauth-creds `
--project=portfolio-492410 `
--role=roles/secretmanager.secretAccessor `
--member="serviceAccount:drive-reader@portfolio-492410.iam.gserviceaccount.com"
```

- `roles/secretmanager.secretAccessor`: allows the service account to read secret versions, but not manage or delete them.

---

## 5. Deploy Cloud Function

Deploy `sync-drive-to-gcs` as a gen2 HTTP-triggered Cloud Function:

```
gcloud functions deploy sync-drive-to-gcs `
--gen2 `
--runtime python311 `
--region asia-south1 `
--source . `
--entry-point sync_drive_to_gcs `
--trigger-http `
--no-allow-unauthenticated `
--service-account drive-reader@portfolio-492410.iam.gserviceaccount.com `
--env-vars-file env.yaml `
--memory 1024MB `
--timeout 540s `
--max-instances 1
```

- `--gen2`: deploys as a 2nd-gen function, which runs on Cloud Run under the hood and supports longer timeouts and more memory.
- `--runtime`: Python version to run the function on.
- `--source`: local directory to upload as the function source.
- `--entry-point`: the specific Python function to invoke when the HTTP trigger fires.
- `--trigger-http`: exposes the function via an HTTPS endpoint.
- `--no-allow-unauthenticated`: restricts access so only callers with a valid identity token can invoke it.
- `--service-account`: the identity the function runs as: determines what GCP resources it can access.
- `--env-vars-file`: YAML file containing environment variables injected at runtime.
- `--memory`: RAM allocated to each function instance.
- `--timeout`: maximum allowed execution time before the function is forcibly terminated.
- `--max-instances`: maximum number of function instances that can run simultaneously. Setting this to `1` ensures only one sync runs at a time, preventing race conditions where multiple instances upload the same files concurrently.

---

## 6. Set Up Cloud Scheduler

Grant `drive-reader` permission to invoke the function:

```
gcloud functions add-invoker-policy-binding sync-drive-to-gcs `
--region=asia-south1 `
--member="serviceAccount:drive-reader@portfolio-492410.iam.gserviceaccount.com"
```

Create a scheduler job that triggers the function every 6 hours:

```
gcloud scheduler jobs create http sync-drive-job `
--location=asia-south1 `
--schedule="0 */6 * * *" `
--uri="$(gcloud functions describe sync-drive-to-gcs --region=asia-south1 --gen2 --format='value(serviceConfig.uri)')" `
--http-method=POST `
--oidc-service-account-email=drive-reader@portfolio-492410.iam.gserviceaccount.com `
--oidc-token-audience="$(gcloud functions describe sync-drive-to-gcs --region=asia-south1 --gen2 --format='value(serviceConfig.uri)')" `
--attempt-deadline=540s
```

- `--schedule`: cron expression defining when the job runs (`0 */6 * * *` = every 6 hours).
- `--uri`: the HTTPS endpoint to call: fetched dynamically from the deployed function's config.
- `--http-method`: HTTP verb used when calling the function.
- `--oidc-service-account-email`: service account used to generate the OIDC identity token attached to the request.
- `--oidc-token-audience`: the audience the token is issued for: must match the function's URL for authentication to succeed.
- `--attempt-deadline`: maximum time the scheduler will wait for the target to respond before marking the attempt as failed. Should match or exceed the function's `--timeout` to prevent the scheduler from giving up before the function finishes.

To run the scheduler on command:

```
gcloud scheduler jobs run sync-drive-job --location="asia-south1"
```