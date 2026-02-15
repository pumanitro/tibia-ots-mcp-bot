"use client";

import { useEffect, useState, useCallback } from "react";
import { fetchState, type BotState } from "./api";

const POLL_INTERVAL = 1500;

export function useBot() {
  const [state, setState] = useState<BotState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await fetchState();
      setState(data);
      setError(null);
    } catch {
      setError("Cannot reach bot API");
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_INTERVAL);
    return () => clearInterval(id);
  }, [refresh]);

  return { state, error, refresh };
}
