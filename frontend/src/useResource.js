import { useEffect, useState } from "react";
import { request } from "./api.js";

export default function useResource(url, revision = 0) {
  const [state, setState] = useState({
    key: null,
    data: null,
    error: "",
    loading: true,
  });
  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    let current = true;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 60000);
    setState((prev) => ({
      key: url,
      data: prev.key === url ? prev.data : null,
      loading: true,
      error: "",
    }));
    request(url, { signal: controller.signal })
      .then((data) => {
        if (current) setState({ key: url, data, error: "", loading: false });
      })
      .catch((error) => {
        if (current)
          setState((prev) => ({
            ...prev,
            loading: false,
            error: timedOut ? "请求超时" : error.message,
          }));
      })
      .finally(() => window.clearTimeout(timer));
    return () => {
      current = false;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [url, revision]);
  return state.key === url ? state : { data: null, loading: true, error: "" };
}
