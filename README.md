# India Call Center Voice Agent - Azure Voice Live with BYOM

A production-ready voice agent accelerator for **Indian call centers**, built with **Azure Voice Live API** and **Bring Your Own Model (BYOM)**. The agent is designed to be **resident in India** (deployed to Azure Central India region), speaks **Hindi** by default, and integrates with Indian telephony providers for PSTN calls.

This solution demonstrates how to build a real-time, low-latency speech-to-speech voice agent that handles phone banking scenarios — verifying customer identity, retrieving account information, and providing loan details — entirely through natural Hindi conversation.

---

## Key Highlights

| Feature | Details |
|---------|---------|
| **Azure Region** | Central India (`centralindia`) — data residency in India |
| **Primary Language** | Hindi (`hi-IN`) with English fallback |
| **AI Model** | GPT-4.1 via BYOM (`byom-azure-openai-chat-completion`) |
| **Voice** | `hi-IN-AnanyaNeural` (Azure TTS, female Hindi voice) |
| **Telephony** | Azure Communication Services + alternative Indian providers (see below) |
| **Deployment** | Azure Container Apps with Managed Identity |
| **Infrastructure** | Bicep/ARM templates, deployed via Azure Developer CLI (`azd`) |

---

## Architecture

```
┌────────────────┐       ┌──────────────────────────┐
│  Phone Call     │       │  Web Browser (Test Mode)  │
│  (PSTN/SIP)    │       │  Microphone + Speaker     │
└───────┬────────┘       └────────────┬──────────────┘
        │                             │
        ▼                             ▼
┌───────────────────────────────────────────────────────┐
│          Azure Communication Services (ACS)           │
│          Event Grid → Incoming Call Webhook            │
└───────────────────────┬───────────────────────────────┘
                        │ WebSocket (bidirectional audio)
                        ▼
┌───────────────────────────────────────────────────────┐
│              Azure Container App (Python/Quart)       │
│  ┌─────────────────────────────────────────────────┐  │
│  │  server.py → ACS Event Handler → Media Handler  │  │
│  │  ↕ WebSocket to Azure Voice Live API             │  │
│  │  ↕ BYOM → GPT-4.1 (Azure OpenAI)               │  │
│  │  ↕ Ambient Mixer (optional background audio)    │  │
│  └─────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────┘
```

### How a Call Works

1. **Incoming call** arrives via ACS (or alternative telephony) → triggers Event Grid webhook
2. **Server answers** the call and sets up bidirectional audio streaming via WebSocket
3. **Audio is streamed** to Azure Voice Live API, which orchestrates ASR → LLM → TTS in a single low-latency pipeline
4. **BYOM routing** sends the LLM request to your own Azure OpenAI deployment (GPT-4.1)
5. **AI agent responds** in Hindi with the `hi-IN-AnanyaNeural` voice
6. **Optional ambient audio** (office/call center background sounds) is mixed in for realism

---

## What is Azure Voice Live API?

The [Azure Voice Live API](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live) is a unified solution enabling low-latency, high-quality speech-to-speech interactions for voice agents. It eliminates the need to manually orchestrate multiple components by integrating:

- **Automatic Speech Recognition (ASR)** — real-time transcription
- **Generative AI (LLM)** — conversation intelligence via your own model (BYOM)
- **Text-to-Speech (TTS)** — natural-sounding voice responses

### What is BYOM (Bring Your Own Model)?

BYOM mode allows you to connect the Voice Live API to **your own Azure OpenAI model deployment** instead of using a default model. Benefits:

- **Control**: Use your preferred model version (e.g., GPT-4.1, GPT-4o)
- **Data residency**: Keep model inference within your chosen Azure region
- **Custom configuration**: Apply your own system prompts, temperature, and token limits
- **Cost management**: Use your own Azure OpenAI quota

In this accelerator, BYOM is configured via the `byom-azure-openai-chat-completion` profile, routing to an Azure AI Foundry endpoint.

---

## Azure Services Used

| Service | Purpose |
|---------|---------|
| [Azure Voice Live API](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live) | Real-time speech-to-speech pipeline (ASR + LLM + TTS) |
| [Azure OpenAI Service](https://learn.microsoft.com/azure/ai-services/openai/) | GPT-4.1 model via BYOM for conversation intelligence |
| [Azure Communication Services](https://learn.microsoft.com/azure/communication-services/) | Telephony integration, call automation, media streaming |
| [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/) | Hosts the Python voice agent application |
| [Azure Container Registry](https://learn.microsoft.com/azure/container-registry/) | Stores Docker images |
| [Azure Key Vault](https://learn.microsoft.com/azure/key-vault/) | Securely stores ACS connection strings |
| [Azure Managed Identity](https://learn.microsoft.com/azure/active-directory/managed-identities-azure-resources/) | Passwordless authentication to all Azure services |
| [Azure Log Analytics + Application Insights](https://learn.microsoft.com/azure/azure-monitor/) | Monitoring and diagnostics |

---

## Using Indian Phone Numbers with Alternative Telephony Providers

While this accelerator uses **Azure Communication Services (ACS)** for telephony, ACS currently has limited availability of Indian PSTN phone numbers. You can integrate alternative telephony providers that offer Indian phone numbers via **SIP trunking** or **direct routing**.

### Supported Indian Telephony Providers

| Provider | Indian Numbers | Integration Method | Notes |
|----------|---------------|-------------------|-------|
| **Exotel** | Yes (local + toll-free) | SIP Trunk / API | Popular in India, supports IVR and call routing |
| **Ozonetel** | Yes (local + toll-free) | SIP Trunk / WebSocket | Cloud contact center, Indian presence |
| **Knowlarity** | Yes (virtual numbers) | SIP Trunk / API | Indian virtual numbers with smart IVR |
| **Tata Communications** | Yes (DID numbers) | SIP Trunk | Enterprise-grade SIP trunking across India |
| **Airtel IQ** | Yes (local + toll-free) | SIP Trunk / API | Airtel's cloud communication platform |
| **Route Mobile** | Yes (local + toll-free) | SIP Trunk | Indian CPaaS with global reach |
| **Twilio** | Yes (limited Indian numbers) | SIP Trunk / Elastic SIP | Global platform with some Indian number availability |

### How to Integrate Alternative Providers

The voice agent architecture supports any telephony provider that can deliver audio via **WebSocket** or **SIP**:

1. **SIP Trunking with ACS Direct Routing**: Configure your Indian telephony provider as a SIP trunk connected to ACS via [Direct Routing](https://learn.microsoft.com/azure/communication-services/concepts/telephony/direct-routing-infrastructure). The call flow remains the same — ACS handles the WebSocket audio bridge to your Container App.

2. **Direct WebSocket Integration**: For providers that support WebSocket audio streaming (e.g., Exotel, Ozonetel), you can bypass ACS entirely and connect the provider's audio stream directly to the `/web/ws` or a custom WebSocket endpoint on your Container App.

3. **AudioHook Integration**: The Voice Live API also supports [AudioHook](https://learn.microsoft.com/azure/ai-services/speech-service/how-to-use-audiohook) for connecting to third-party contact center platforms like Genesys.

```
┌──────────────────────────┐
│  Indian Telephony Provider│
│  (Exotel / Ozonetel /    │
│   Knowlarity / Airtel IQ)│
└──────────┬───────────────┘
           │
           ▼  Option A: SIP Trunk
┌──────────────────────────┐
│  ACS Direct Routing      │──► Container App ──► Voice Live API
└──────────────────────────┘
           │
           ▼  Option B: Direct WebSocket
┌──────────────────────────┐
│  Container App /web/ws   │──► Voice Live API (BYOM → GPT-4.1)
└──────────────────────────┘
```

---

## Getting Started

### Prerequisites

- [Azure Subscription](https://azure.microsoft.com/free/) with permissions to create resources
- [Azure Developer CLI (azd)](https://aka.ms/install-azd)
- [Azure CLI (az)](https://learn.microsoft.com/cli/azure/)
- [Python 3.12+](https://www.python.org/)
- [UV](https://docs.astral.sh/uv/getting-started/installation/)

### Quick Deploy to Azure

```bash
# Clone this repo
git clone https://github.com/ss4aman/india-call-center-voice-agent.git
cd india-call-center-voice-agent

# Login to Azure
azd auth login

# Deploy (select 'centralindia' when prompted for region)
azd up
```

During deployment you'll be prompted for:
- **Environment name**: e.g., `india-cc-agent`
- **Azure subscription**: Your subscription
- **Azure location**: Choose `centralindia` for India data residency

### Configuration

After deployment, configure BYOM and Voice Live settings:

```bash
# Set BYOM mode to route to your own Azure OpenAI deployment
azd env set VOICELIVE_BYOM_MODE byom-azure-openai-chat-completion
azd env set VOICELIVE_FOUNDRY_RESOURCE <your-foundry-resource-name>

# Optionally reuse an existing Voice Live endpoint
azd env set EXISTING_VOICE_LIVE_ENDPOINT https://<your-resource>.cognitiveservices.azure.com/

# Optionally reuse an existing ACS resource
azd env set EXISTING_ACS_CONNECTION_STRING "endpoint=https://...;accesskey=..."

# Redeploy with new settings
azd up
```

### Voice & Language Configuration

Set these environment variables to customize the agent's behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `AZURE_VOICELIVE_INPUT_LANGUAGE` | `hi-IN` | Input language (Hindi) |
| `AZURE_VOICELIVE_INPUT_TRANSCRIPTION_MODEL` | `azure-speech` | ASR model (`azure-speech` or `whisper-1`) |
| `AZURE_VOICELIVE_VAD_TYPE` | `azure_semantic_vad` | Voice Activity Detection type |
| `AZURE_VOICELIVE_NOISE_REDUCTION_TYPE` | `azure_deep_noise_suppression` | Background noise reduction |
| `AZURE_VOICELIVE_ECHO_CANCELLATION_ENABLED` | `True` | Echo cancellation |
| `AMBIENT_PRESET` | `none` | Background audio (`none`, `office`, `call_center`) |

---

## Testing

### Web Browser (Quick Test)

1. After deployment, find the **Container App URL** in the Azure Portal
2. Open the URL in your browser
3. Click **Start Talking to Agent** — speak in Hindi to interact with the banking agent
4. Click **Stop Conversation** to end

### Phone Call (Production Scenario)

1. In your **Azure Communication Services** resource, go to **Events**
2. Create an **Event Subscription** for `IncomingCall` events pointing to:
   ```
   https://<your-container-app-url>/acs/incomingcall
   ```
3. [Get a phone number](https://learn.microsoft.com/azure/communication-services/quickstarts/telephony/get-phone-number) or configure SIP Direct Routing with an Indian telephony provider
4. Call the number — the AI agent will answer in Hindi

---

## Project Structure

```
├── azure.yaml              # Azure Developer CLI service definitions
├── local-overrides.env      # Local config overrides (gitignored)
├── infra/                   # Bicep infrastructure-as-code
│   ├── main.bicep           # Main deployment template
│   ├── main.parameters.json # Parameterized deployment config
│   └── modules/             # Modular Bicep (ACS, AI, Container App, etc.)
├── server/                  # Python voice agent application
│   ├── server.py            # Quart web server (HTTP + WebSocket endpoints)
│   ├── Dockerfile           # Container build definition
│   ├── pyproject.toml       # Python dependencies
│   ├── app/
│   │   ├── data/            # Mock bank data (JSON)
│   │   ├── audio/           # Ambient audio files (WAV)
│   │   └── handler/
│   │       ├── acs_event_handler.py   # ACS call event processing
│   │       ├── acs_media_handler.py   # Voice Live WebSocket + audio streaming
│   │       └── ambient_mixer.py       # Background audio mixing
│   └── static/
│       ├── index.html        # Web test client
│       └── audio-processor.js # Browser audio worklet
```

---

## Mock Banking Scenario

The included demo agent acts as a **Hindi-speaking phone banking assistant** for a fictional bank. It:

- Greets callers in Hindi
- Verifies identity (account ID, mobile last 4 digits, date of birth)
- Provides account balance, recent transactions, and loan status
- Uses mock data from `server/app/data/` — easily replaceable with your own data or API integrations

---

## Customization

### Change the Agent's Persona
Edit the system instructions in `server/app/handler/acs_media_handler.py` — the `_build_puri_bank_instructions()` function, or set the `BANK_SYSTEM_INSTRUCTIONS` environment variable to override.

### Change the Voice
Set `BANK_VOICE_NAME` environment variable. Available Hindi voices include:
- `hi-IN-AnanyaNeural` (female, default)
- `hi-IN-SwaraNeural` (female)
- `hi-IN-MadhurNeural` (male)
- See [full list of Azure Neural TTS voices](https://learn.microsoft.com/azure/ai-services/speech-service/language-support)

### Use a Different Model
Change `VOICE_LIVE_MODEL` to any Azure OpenAI model that supports realtime/chat completions.

### Ambient Background Audio

Add realistic background audio to simulate a call center environment:

| Preset | Description |
|--------|-------------|
| `none` | No background audio (default) |
| `office` | Quiet office ambient |
| `call_center` | Busy call center background |

```bash
azd env set AMBIENT_PRESET call_center
azd deploy
```

---

## Security Considerations

- **Managed Identity**: All Azure service-to-service authentication uses User Assigned Managed Identity (no API keys stored in code)
- **Key Vault**: ACS connection string is stored in Azure Key Vault and injected via secret reference
- **No hardcoded secrets**: All sensitive values are parameterized through `azd` environment variables
- **Data residency**: Deploy to `centralindia` to keep all data within India

---

## Resources

- [Azure Voice Live API Documentation](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live)
- [Azure Voice Live BYOM Guide](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live-byom)
- [Azure Communication Services Call Automation](https://learn.microsoft.com/azure/communication-services/concepts/call-automation/call-automation)
- [Azure AI Foundry](https://ai.azure.com/foundry)
- [ACS Direct Routing for SIP Trunking](https://learn.microsoft.com/azure/communication-services/concepts/telephony/direct-routing-infrastructure)

---

## Cleanup

```bash
azd down
```

This removes all Azure resources created by the deployment.

---

## License

This project is licensed under the MIT License - see [LICENSE.md](LICENSE.md) for details.

Based on the [Azure Samples Call Center Voice Agent Accelerator](https://github.com/Azure-Samples/call-center-voice-agent-accelerator).
