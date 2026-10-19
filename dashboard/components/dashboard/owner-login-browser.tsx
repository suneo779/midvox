"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function OwnerLoginBrowser({ service }: { service: "youtube" | "discord" }) {
  const label = service === "youtube" ? "YouTube" : "Discord";
  const endpoint = `/api/${service}-login`;
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(`Open the Railway browser and sign in to ${label} yourself.`);
  const [text, setText] = useState("");
  const [image, setImage] = useState("");
  const imageUrl = useRef("");
  const refreshing = useRef(false);
  const browserInput = useRef<HTMLDivElement>(null);
  const actionQueue = useRef<Promise<void>>(Promise.resolve());
  const typedText = useRef("");
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      const response = await fetch(endpoint, { cache: "no-store" });
      if (!response.ok) {
        const error = await response.json();
        setMessage(error.detail);
        if (error.reopen || response.status === 401 || response.status === 403) {
          setRunning(false);
          setImage("");
        }
        return;
      }
      const url = URL.createObjectURL(await response.blob());
      if (imageUrl.current) URL.revokeObjectURL(imageUrl.current);
      imageUrl.current = url;
      setImage(url);
    } finally {
      refreshing.current = false;
    }
  };

  const queueAction = (action: string, values: Record<string, unknown> = {}) => {
    const next = actionQueue.current.then(async () => {
      setBusy(true);
      try {
        const response = await fetch(endpoint, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, ...values }),
        });
        const result = await response.json();
        setMessage(result.detail || result.message);
        if (!response.ok) {
          if (result.reopen) { setRunning(false); setImage(""); }
          return;
        }
        if (action === "status") {
          setRunning(Boolean(result.running));
          if (result.running) await refresh();
        } else if (action === "stop" || action === "save") {
          setRunning(false);
          setImage("");
        } else {
          setRunning(true);
          await refresh();
        }
      } catch {
        setMessage("Could not reach the Railway browser. Try opening it again.");
      } finally {
        setBusy(false);
      }
    });
    actionQueue.current = next;
    return next;
  };

  const flushTyping = () => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    typingTimer.current = null;
    const pending = typedText.current;
    typedText.current = "";
    if (pending) void queueAction("text", { text: pending });
  };

  const action = (name: string, values: Record<string, unknown> = {}) => {
    flushTyping();
    return queueAction(name, values);
  };

  useEffect(() => { void queueAction("status"); }, [service]);

  useEffect(() => {
    if (!running || busy) return;
    const timer = setInterval(() => { refresh().catch(() => setMessage("Browser connection interrupted.")); }, 2500);
    return () => clearInterval(timer);
  }, [running, busy]);

  useEffect(() => () => {
    if (imageUrl.current) URL.revokeObjectURL(imageUrl.current);
    if (typingTimer.current) clearTimeout(typingTimer.current);
    typedText.current = "";
  }, []);

  return <section className="mx-auto max-w-6xl space-y-5 p-6">
    <h1 className="text-2xl font-bold">{label} login on Railway</h1>
    <p className="text-sm text-[var(--panel-muted)]">Click a field inside the browser, then type or paste directly. Enter, Tab, Backspace, arrow keys and Ctrl+A work inside it. Press Escape to leave keyboard control. Complete any verification yourself, then save the session.</p>
    <div className="flex flex-wrap gap-2">
      <Button disabled={busy} onClick={() => action("start")}>Open browser</Button>
      <Button variant="outline" disabled={busy || !running} onClick={() => action(service)}>Return to {label}</Button>
      <Button disabled={busy || !running} onClick={() => action("save")}>Save session and close</Button>
      <Button variant="outline" disabled={busy || !running} onClick={() => action("stop")}>Close browser</Button>
    </div>
    <p role="status" className="text-sm">{busy ? "Working…" : message}</p>
    {running && <>
      <form className="flex flex-wrap gap-2" onSubmit={event => {
        event.preventDefault(); action("text", { text }); setText("");
      }}>
        <Input type="password" autoComplete="off" value={text} onChange={event => setText(event.target.value)} placeholder="Alternative: text for the selected browser field" className="max-w-md" />
        <Button type="submit" disabled={busy || !text}>Type into browser</Button>
        {["Enter", "Tab", "Backspace"].map(key => <Button type="button" variant="outline" disabled={busy} key={key} onClick={() => action("key", { key })}>{key}</Button>)}
        <Button type="button" variant="outline" disabled={busy} onClick={() => action("scroll", { delta: 500 })}>Scroll down</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={() => action("scroll", { delta: -500 })}>Scroll up</Button>
      </form>
      {image && <div ref={browserInput} tabIndex={0} role="application" aria-label={`Remote ${label} login browser. Click a field and type. Press Escape to leave keyboard control.`} className="rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-[#B9CF7A]" onPaste={event => {
        event.preventDefault();
        void action("text", { text: event.clipboardData.getData("text") });
      }} onKeyDown={event => {
        if (event.nativeEvent.isComposing) return;
        if (event.key === "Escape") {
          event.preventDefault();
          void action("key", { key: "Escape" });
          event.currentTarget.blur();
        } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
          event.preventDefault();
          void action("key", { key: "a", modifiers: 2 });
        } else if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1) {
          event.preventDefault();
          typedText.current += event.key;
          if (typingTimer.current) clearTimeout(typingTimer.current);
          typingTimer.current = setTimeout(flushTyping, 120);
        } else if (["Enter", "Tab", "Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          void action("key", { key: event.key, modifiers: event.shiftKey ? 8 : 0 });
        }
      }}><img src={image} alt={`Live ${label} browser on Railway — click a field and type`} className="w-full rounded-xl border cursor-pointer" draggable={false} onClick={event => {
        browserInput.current?.focus({ preventScroll: true });
        const box = event.currentTarget.getBoundingClientRect();
        action("click", { x: (event.clientX - box.left) * 1100 / box.width, y: (event.clientY - box.top) * 800 / box.height });
      }} /></div>}
    </>}
    <p className="text-xs text-[var(--panel-muted)]">{service === "discord" ? "Saving selects this Discord account for bumping instead of USER_TOKEN. " : ""}Only one login browser runs at a time; opening this one closes the other. Saved sessions stay private and are backed up encrypted in MongoDB. Keep the same database, NEXTAUTH_SECRET, and primary OWNER_IDS entry when moving hosts. The provider may expire or revoke a session, requiring another login.</p>
  </section>;
}
