#!/usr/bin/env bash
set -euo pipefail

echo "=== Google Cloud Setup for Sheets API ==="
echo ""
echo "This script guides you through creating a Google Cloud service account."
echo "You'll need a Google account and a web browser."
echo ""

# Check for gcloud CLI (optional, manual steps provided as fallback)
if command -v gcloud &>/dev/null; then
    echo "gcloud CLI detected. Using automated setup."
    echo ""

    PROJECT_ID="hermes-expense-tracker"

    echo "Step 1: Creating project '$PROJECT_ID'..."
    gcloud projects create "$PROJECT_ID" --name="Hermes Expense Tracker" 2>/dev/null || \
        echo "  Project may already exist, continuing..."
    gcloud config set project "$PROJECT_ID"

    echo "Step 2: Enabling APIs..."
    gcloud services enable sheets.googleapis.com
    gcloud services enable drive.googleapis.com
    echo "  Sheets API and Drive API enabled."

    echo "Step 3: Creating service account..."
    SA_NAME="hermes-sheets"
    SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
    gcloud iam service-accounts create "$SA_NAME" \
        --display-name="Hermes Sheets Access" 2>/dev/null || \
        echo "  Service account may already exist, continuing..."

    echo "Step 4: Downloading key..."
    KEY_PATH="$HOME/.hermes/service-account.json"
    mkdir -p "$HOME/.hermes"
    gcloud iam service-accounts keys create "$KEY_PATH" \
        --iam-account="$SA_EMAIL"
    echo "  Key saved to: $KEY_PATH"

    echo ""
    echo "=== Automated setup complete ==="
    echo ""
    echo "Service account email: $SA_EMAIL"
    echo "Key file: $KEY_PATH"
    echo ""
    echo "Next: Share your Google Sheet with $SA_EMAIL (Editor access)"

else
    echo "gcloud CLI not found. Follow these manual steps:"
    echo ""
    echo "1. Go to https://console.cloud.google.com/"
    echo "2. Create a new project (e.g., 'Hermes Expense Tracker')"
    echo "3. Go to APIs & Services → Library"
    echo "4. Search for and enable:"
    echo "   - Google Sheets API"
    echo "   - Google Drive API"
    echo "5. Go to APIs & Services → Credentials"
    echo "6. Click 'Create Credentials' → 'Service Account'"
    echo "   - Name: hermes-sheets"
    echo "   - Role: none needed (we share the sheet directly)"
    echo "7. Click on the created service account"
    echo "8. Go to 'Keys' tab → 'Add Key' → 'Create new key' → JSON"
    echo "9. Save the downloaded JSON file to: ~/.hermes/service-account.json"
    echo ""
    echo "Then update ~/.hermes/.env:"
    echo "  GOOGLE_SERVICE_ACCOUNT_JSON=~/.hermes/service-account.json"
    echo ""
    echo "Finally, share your Google Sheet with the service account email"
    echo "(found in the JSON file under 'client_email') with Editor access."
fi
