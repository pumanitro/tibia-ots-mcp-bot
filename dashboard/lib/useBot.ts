"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { fetchState, type BotState } from "./api";

const WS_URL = "ws://127.0.0.1:8090";
const HTTP_FALLBACK_INTERVAL = 500;

export function useBot() {
  const [state, setState] = useState<BotState | null>(null);
  const [mcpConnected, setMcpConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const cancelledRef = useRef(false);

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
    cancelledRef.current = false;
    let fallbackTimer: ReturnType<typeof setTimeout> | null = null;

    function connectWs() {
      if (cancelledRef.current) return;

      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setMcpConnected(true);
        // Stop HTTP fallback while WS is alive
        if (fallbackTimer) {
          clearTimeout(fallbackTimer);
          fallbackTimer = null;
        }
      };

      ws.onmessage = (event) => {
        try {
          const data: BotState = JSON.parse(event.data);
          setState(data);
          setMcpConnected(true);
        } catch {}
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (!cancelledRef.current) {
          // Fall back to HTTP polling, retry WS after 2s
          startHttpFallback();
          setTimeout(connectWs, 2000);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    function startHttpFallback() {
      if (fallbackTimer || cancelledRef.current) return;
      async function poll() {
        if (cancelledRef.current || wsRef.current?.readyState === WebSocket.OPEN) return;
        await refresh();
        if (!cancelledRef.current && wsRef.current?.readyState !== WebSocket.OPEN) {
          fallbackTimer = setTimeout(poll, HTTP_FALLBACK_INTERVAL);
        }
      }
      poll();
    }

    connectWs();

    return () => {
      cancelledRef.current = true;
      if (fallbackTimer) clearTimeout(fallbackTimer);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [refresh]);

  return { state, mcpConnected, refresh };
}
