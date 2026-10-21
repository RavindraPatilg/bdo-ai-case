# Azure Container Apps deployment guide (Windows / Azure CLI)

**Deployment has not been run.** This file is the original Azure deployment plan, prepared before the live deployment moved to Render (see README\_deployment.md). These are instructions for deploying under your own Azure subscription. You need an Azure subscription and a real LLM provider key. Do not put keys in Git, source files, or chat.

## A. Prepare Azure CLI

1. Install Azure CLI from Microsoft, if permitted on your work laptop. Otherwise use Azure Cloud Shell in the Azure portal.
2. Open PowerShell or Azure Cloud Shell and run:

```powershell
az login
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
```

## B. Build and deploy from the project folder

From the folder containing the Dockerfile, run (replace names if already taken):

```powershell
az containerapp up --name bdo-ai-analyser --resource-group bdo-ai-case-rg --location westeurope --source . --ingress external --target-port 8000
```

The command builds the Docker image and creates the required Container Apps resources. Save the HTTPS URL it prints. Confirm it responds at `/health`.

## C. Add the LLM key safely

In Azure Portal, open the Container App, then **Secrets**. Add a secret named `llm-api-key` with the real provider key. Do not paste the key into a checked-in file or into this chat.

In **Revisions and replicas / Containers / Environment variables**, add:

* `LLM\_PROVIDER` = `openai`
* `OPENAI\_API\_KEY` = reference to secret `llm-api-key`
* `OPENAI\_MODEL` = the model id of your chosen provider
* `RATE\_LIMIT\_PER\_HOUR` = `10`
* `ENVIRONMENT` = `production`
* `APP\_ACCESS\_TOKEN` = optional, preferably a separate secret if you enable it

If using an OpenAI-compatible provider endpoint, set `OPENAI\_BASE\_URL` to its documented endpoint. Save and create a new revision. Test the URL with one case orgnr.

## D. Durable saved analyses

The default SQLite file is local to the container and can be lost when a container is replaced. For this case, configure an Azure Files volume and mount it into the container, then set `DATABASE\_PATH` to a file in that mount, for example `/mnt/data/analyses.db`. Azure Files setup depends on your subscription permissions. If you cannot set it up, disclose the ephemeral SQLite limitation in the README and ensure the saved example outputs are also provided as `docs/saved\_outputs.json`.

## Cost and security notes

Free allowances were checked before deployment; no paid resources are used
(the live deployment runs on free tiers, see README\_deployment.md). Never commit or publish the provider key.

