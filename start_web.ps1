$ErrorActionPreference = "Stop"
$HostAddress = if ($env:HOST) { $env:HOST } else { "0.0.0.0" }
$Port = if ($env:PORT) { $env:PORT } else { "18083" }
uvicorn app:app --host $HostAddress --port $Port
