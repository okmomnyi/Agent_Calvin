import { useEffect, useState } from "react";
import { AppFrame } from "@/ui/shell/AppFrame";
import { HudStatesDevRoute } from "@/dev/HudStates";
import { detectCapabilities, type Capabilities } from "@/core/capabilities";

function isDevHudRoute(): boolean {
  return import.meta.env.DEV && new URLSearchParams(location.search).get("dev") === "hud";
}

export function App() {
  const [caps, setCaps] = useState<Capabilities | null>(null);

  useEffect(() => {
    detectCapabilities().then(setCaps);
  }, []);

  if (isDevHudRoute()) return <HudStatesDevRoute />;

  // Capability detection is async (it may wait on pywebviewready); render the web-shaped
  // frame immediately rather than a blank screen; it self-corrects once caps resolve.
  return <AppFrame shell={caps?.shell ?? "web"} />;
}
