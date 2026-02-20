"use client";

import { useEffect, useState, useCallback } from "react";
import { fetchState, type BotState } from "./api";

const POLL_INTERVAL = 500;

export function useBot() {
  const [state, setState] = useState<BotState | null>(null);
  const [mcpConnected, setMcpConnected] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await fetchState();
      setState(data);
      setMcpConnected(true);
    } catch {
      setState(null);
      setMcpConnected(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_INTERVAL);
    return () => clearInterval(id);
  }, [refresh]);

  return { state, mcpConnected, refresh };
}
