# Beta deployment to `saauxfotos`

This workflow publishes the existing Shifting Tiles gallery from **`beta`** to the Azure Storage account **`saauxfotos`**. It does not change production workflows, deploy from `master` or `main`, or implement OneDrive synchronization yet.

The files are prepared locally only. No Azure configuration, GitHub settings, commit, push, or deployment has been performed. Complete the setup below before separately approving a beta push and deployment.

## 1. Enable the Storage static website

In the Azure portal, open **Storage accounts → saauxfotos → Data management → Static website**. Enable static hosting, set the **Index document name** to **`index.html`**, leave the error document blank for now, and save. Azure creates the **`$web`** container. [1]

Copy the **Primary endpoint** displayed on this page. It is the website URL, not the blob endpoint. Use the exact HTTPS URL shown by Azure; do not guess the region/zone suffix. The workflow accepts the public-cloud endpoint for `saauxfotos`, such as `https://saauxfotos.zNN.web.core.windows.net/`, where `NN` must be replaced by the actual Azure value.

This is a public, anonymous website. All files uploaded to its website endpoint are publicly readable. Never put account credentials, sync state, private originals, or private future user libraries in `$web`. Setting the container's blob access level to private does not make its static website endpoint private. [1]

The GitHub-hosted runner needs network access to the account's **blob endpoint** for uploads, and visitors need access to the **website endpoint**. If firewalls or private endpoints restrict either, arrange the intended network access before running deployment; OIDC does not bypass network restrictions.

## 2. Create the GitHub `beta` environment

Open **soyroberto/shiftingtiles → Settings → Environments** and create an environment named **`beta`**.

Under **Deployment branches and tags**, choose **Selected branches and tags** and add a **branch** rule named **`beta`** only. Do not add `master`, `main`, tags, or a wildcard. Optionally add a required reviewer to approve each beta deployment. These controls protect the environment-based Azure federation below. [2]

The workflow also checks the exact repository and `refs/heads/beta`. Its automatic trigger is a push to `beta`; it does not deploy pull requests.

## 3. Create a dedicated Azure deployment identity

In the Microsoft Entra tenant associated with your Azure subscription, open **App registrations → New registration**. Use a name such as **`shiftingtiles-beta-storage-deployer`**, select **Accounts in this organizational directory only**, leave the redirect URI blank, and register the application. Registration through the portal creates the associated service principal. [3]

Record the **Application/client ID** and **Directory/tenant ID**. Also record the **subscription ID containing `saauxfotos`**. You do not need a client secret, certificate, storage key, SAS token, or connection string for this workflow.

This identity is only for GitHub deploying to Azure. It is separate from the future application that will request permission to read your personal OneDrive. Do not add Graph file permissions to the deployment identity.

### Add GitHub federation

In this app registration, open **Certificates & secrets → Federated credentials → Add credential**, then select the scenario for **GitHub Actions deploying Azure resources**. Configure:

- Organization: **`soyroberto`**.
- Repository: **`shiftingtiles`**.
- Entity type: **Environment**.
- Environment: **`beta`**.
- Credential name: **`github-shiftingtiles-beta`**.

For the repository's original GitHub subject format, the resulting values are:

```text
Issuer:   https://token.actions.githubusercontent.com
Subject:  repo:soyroberto/shiftingtiles:environment:beta
Audience: api://AzureADTokenExchange
```

Use the **environment** subject, not `repo:soyroberto/shiftingtiles:ref:refs/heads/beta`: the job declares `environment: beta`. The environment's branch restriction supplies the branch boundary. [3] [4]

GitHub now uses an immutable, ID-bearing default subject for repositories created, renamed, or transferred after 15 July 2026. Older repositories retain the original format unless they opt in. This repository predates that change, but its live OIDC configuration has not been inspected. If it was renamed/transferred, opted into the new format, or uses a customized subject, configure Azure to match its actual `sub` claim instead. An Azure “no matching federated identity record” error can identify the subject that failed. Do not print or share the complete OIDC token. [4]

### Grant access only to the beta website container

Assign the deployment service principal **Storage Blob Data Contributor**, scoped to **`saauxfotos` → `$web`**. This is enough for the blob uploads; the workflow does not query management-plane account settings or enable static hosting. It therefore does not need account-level Reader, account-level Contributor, or subscription-wide privileges. Container-level scope follows Microsoft's least-privilege guidance. [5]

The built-in role includes delete capability, but this workflow does not use it. It contains no blob-delete command and no delete-destination synchronization.

If you prefer Azure Cloud Shell, the following Bash commands assign that role. **These are setup instructions, not commands already executed.** Replace the placeholders first. Run them as an account authorized to assign roles, not as the deployment identity.

```bash
SUBSCRIPTION_ID='<subscription-containing-saauxfotos>'
RESOURCE_GROUP='<resource-group-containing-saauxfotos>'
CLIENT_ID='<application-client-id>'

az account set --subscription "$SUBSCRIPTION_ID"

SP_OBJECT_ID="$(az ad sp show --id "$CLIENT_ID" --query id --output tsv)"
STORAGE_ID="$(az storage account show \
  --name saauxfotos \
  --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)"
WEB_SCOPE="${STORAGE_ID}/blobServices/default/containers/\$web"

az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role 'Storage Blob Data Contributor' \
  --scope "$WEB_SCOPE"
```

If directory lookup is unavailable, obtain the service principal object ID from **Enterprise applications**. Do not confuse it with the app registration object ID or the application/client ID. Role changes can take up to ten minutes to take effect. [5]

## 4. Configure the GitHub environment values

In the GitHub **`beta` environment**, add these three **environment secrets** using the IDs recorded above:

```text
BETA_AZURE_CLIENT_ID
BETA_AZURE_TENANT_ID
BETA_AZURE_SUBSCRIPTION_ID
```

Add an **environment variable** named **`BETA_WEBSITE_URL`** with the exact Primary endpoint copied from the Storage static website page.

These IDs identify Azure configuration; none is a reusable authentication password. The workflow uses short-lived GitHub OIDC tokens and the federated identity relationship to authenticate. Microsoft recommends environment secrets for these values in public repositories. [3] [4]

There is no Storage account-name variable to set: the workflow is deliberately fixed to **`saauxfotos`**, and checks that the website URL also belongs to that account.

## 5. Review and publish only when approved

The implementation consists of:

```text
.github/workflows/beta-storage.yml
.github/scripts/prepare_static_site.py
docs/beta-storage-setup.md
```

Keep `.work/` and `outputs/` out of the commit. They contain local research and validation files and are not deployment dependencies. No existing production workflow needs to be edited or staged.

When separately approved, commit only the three implementation files on `beta` and push to `origin/beta`. The push starts **Deploy beta gallery to Azure Storage**. This workflow being new and present only on `beta` can mean the **Run workflow** button is unavailable until it also exists on the default branch. Use the beta push trigger for the first run; do not modify `master` just to expose manual dispatch. GitHub requires a workflow on the default branch for `workflow_dispatch` events. [8]

The workflow checks its settings, packages the assets, authenticates, uploads dependencies, and publishes `index.html` last. It then checks that the public endpoint returns the deployed index, the scripts, the stylesheet source, and a representative photo. These HTTP checks are not a visual browser test. After deployment, open the endpoint in a signed-out browser and verify the tile animation and keyboard controls.

## What is uploaded and preserved

The packager allows the five required root-level HTML/JavaScript/Less files and Git-tracked root-level image files. It rejects symlinks and missing required files. It excludes `.git`, `.github`, `awk`, `.work`, `outputs`, documentation, backend code, hidden files, and untracked files. It does not copy the repository root wholesale.

Azure CLI uploads use **`--auth-mode login`** and **`--overwrite true`**. Existing files with matching packaged paths are replaced. Unrelated blobs are not deleted. Assets have a five-minute cache lifetime; `index.html` uses `no-cache` and an explicit HTML content type. [6] [7]

Reserve a separate future prefix such as **`photos/onedrive/`** and a generated **`gallery.json`** for the OneDrive publisher. Neither is packaged by this workflow, so a website deployment will leave them intact. Adding that prefix and manifest to the frontend remains a later change.

This first workflow uploads the current repository photos automatically. It does not yet eliminate repository-based photo management; the OneDrive integration is the next stage. It also leaves stale files in place intentionally. Any later cleanup needs a narrowly scoped, separately reviewed policy.

The existing `index.html` references **`f104.jpeg`**, but the repository contains **`f104.jpg`** instead. The packager warns about that missing image and leaves the page unchanged, as requested for this deployment-only step. That pre-existing mismatch can cause an occasional empty tile and should be corrected separately or disappear when the hard-coded image list is replaced.

## If the first run fails

A **missing setting** error means one of the three beta environment secrets or the website URL is absent. A **federated identity mismatch** usually means the subject, issuer, audience, tenant, or client ID does not match; check the environment-based subject and any GitHub subject-format customization.

A **403 from Storage** after successful login can indicate missing container-level data access, role propagation, or network restrictions. A **missing `$web` container** means static hosting setup was not completed. A **404 from the website** can indicate the wrong Primary endpoint, a missing or incorrectly cased `index.html`, or the wrong index-document setting.

The gallery and upload paths are case-sensitive. Do not work around authentication failures by adding Storage keys or broad production privileges. [1] [5]

## References

[1]: https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-static-website "Azure Storage static website hosting"
[2]: https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments "GitHub deployment environments and protection rules"
[3]: https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect "Azure Login with GitHub OpenID Connect"
[4]: https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-azure "GitHub OpenID Connect configuration for Azure"
[5]: https://learn.microsoft.com/en-us/azure/storage/blobs/assign-azure-role-data-access "Assign Azure roles for blob data access"
[6]: https://learn.microsoft.com/en-us/cli/azure/storage/blob?view=azure-cli-latest "Azure CLI blob upload commands"
[7]: https://github.com/Azure/login "Azure Login action and OIDC usage"
[8]: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch "GitHub workflow dispatch events"
