"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import {
  Shield, Users, Server, Activity, Database, Cpu, Globe, Lock, Settings, RefreshCw, Youtube, ArrowUpRight
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { AdminStats, AdminConfig } from "@/types/api";
import { toast } from "sonner";

export function AdminContent({ canManageOwnerLogins = false }: { canManageOwnerLogins?: boolean }) {
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [config, setConfig] = useState<AdminConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notification, setNotification] = useState("");

  const fetchData = async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true);
    try {
      const [statsData, configData] = await Promise.all([
        api.getAdminStats(),
        api.getAdminConfig()
      ]);
      setStats(statsData);
      setConfig(configData);
      setNotification(configData.global_notification || "");
    } catch (err) {
      console.error("Failed to fetch admin data:", err);
      toast.error("Failed to load real-time data");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(() => fetchData(true), 30000); // Auto refresh every 30s
    return () => clearInterval(interval);
  }, []);

  const handleToggleMaintenance = async () => {
    if (!config) return;
    setSaving(true);
    try {
      const newStatus = !config.maintenance_mode;
      await api.updateAdminConfig({ maintenance_mode: newStatus });
      setConfig({ ...config, maintenance_mode: newStatus });
      toast.success(`Maintenance mode ${newStatus ? 'enabled' : 'disabled'}`);
    } catch (err) {
      toast.error("Failed to update maintenance mode");
    } finally {
      setSaving(false);
    }
  };

  const handleBroadcast = async () => {
    setSaving(true);
    try {
      await api.updateAdminConfig({ global_notification: notification });
      if (config) setConfig({ ...config, global_notification: notification });
      toast.success("Broadcast message updated");
    } catch (err) {
      toast.error("Failed to update broadcast message");
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <RefreshCw className="h-10 w-10 text-red-500 animate-spin opacity-20" />
      </div>
    );
  }

  const statItems = [
    { name: "Total Users", value: stats?.total_users || "0", icon: Users, color: "text-blue-500" },
    { name: "Active Servers", value: stats?.active_servers || "0", icon: Server, color: "text-emerald-500" },
    { name: "API Latency", value: stats?.api_latency || "0ms", icon: Activity, color: "text-amber-500" },
    { name: "Database Size", value: stats?.db_size || "0 MB", icon: Database, color: "text-amber-500" },
  ];

  return (
    <div className="space-y-10 animate-in fade-in duration-500">
      {/* Header */}
      <div className="relative group">
        <div className="absolute -inset-1 bg-gradient-to-r from-red-500 to-primary-dark rounded-[6px] blur opacity-10 group-hover:opacity-20 transition duration-1000"></div>
        <div className="relative bg-[#0F0F0F] border border-[#1F1F1F] rounded-[6px] p-8 lg:p-12 flex flex-col lg:flex-row lg:items-center justify-between gap-8">
          <div className="flex items-center gap-6">
            <div className="h-16 w-16 rounded-[6px] bg-red-500/20 flex items-center justify-center border border-red-500/30 shadow-2xl shadow-red-500/20">
              <Shield className="h-8 w-8 text-red-500" />
            </div>
            <div>
              <h1 className="text-4xl font-black text-slate-50 tracking-tight font-display">Admin Control Panel</h1>
              <p className="text-neutral-400 mt-2 font-medium">Restricted access for MidVox administrators only.</p>
            </div>
          </div>
          <button
            onClick={() => fetchData(true)}
            className="flex items-center gap-3 bg-red-500/5 px-6 py-3 rounded-[6px] border border-red-500/10 hover:bg-red-500/10 transition-all active:scale-95 group/refresh"
          >
            <RefreshCw className={cn("h-4 w-4 text-red-500 transition-all", refreshing && "animate-spin")} />
            <span className="text-xs font-black uppercase tracking-widest text-red-500">
              {refreshing ? "Refreshing..." : "Real-time Mode"}
            </span>
          </button>
        </div>
      </div>

      {canManageOwnerLogins && (
        <section className="flex flex-col gap-5 rounded-[6px] border border-[#B9CF7A]/25 bg-[#131710] p-6 sm:flex-row sm:items-center sm:justify-between" aria-labelledby="youtube-login-heading">
          <div className="flex items-start gap-4">
            <Youtube className="mt-1 h-6 w-6 shrink-0 text-[#B9CF7A]" aria-hidden="true" />
            <div>
              <h2 id="youtube-login-heading" className="text-lg font-bold text-slate-50">YouTube login</h2>
              <p className="mt-1 text-sm text-neutral-400">Manage the bot&apos;s YouTube session for Shorts downloads. Bot owners only.</p>
            </div>
          </div>
          <Link href="/dashboard/youtube-login" className="inline-flex shrink-0 items-center justify-center gap-2 rounded-md bg-[#B9CF7A] px-5 py-3 text-sm font-semibold text-[#151810] transition-colors hover:bg-[#C6DB8B] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#B9CF7A]">
            Open YouTube login <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
          </Link>
        </section>
      )}

      {canManageOwnerLogins && (
        <section className="flex flex-col gap-5 rounded-[6px] border border-[#B9CF7A]/25 bg-[#131710] p-6 sm:flex-row sm:items-center sm:justify-between" aria-labelledby="discord-login-heading">
          <div className="flex items-start gap-4">
            <Users className="mt-1 h-6 w-6 shrink-0 text-[#B9CF7A]" aria-hidden="true" />
            <div>
              <h2 id="discord-login-heading" className="text-lg font-bold text-slate-50">Discord login</h2>
              <p className="mt-1 text-sm text-neutral-400">Sign in to the Discord account used for bumping. Saved sessions are encrypted. Bot owners only.</p>
            </div>
          </div>
          <Link href="/dashboard/discord-login" className="inline-flex shrink-0 items-center justify-center gap-2 rounded-md bg-[#B9CF7A] px-5 py-3 text-sm font-semibold text-[#151810] transition-colors hover:bg-[#C6DB8B] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#B9CF7A]">
            Open Discord login <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
          </Link>
        </section>
      )}

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {statItems.map((stat) => (
          <div key={stat.name} className="bg-[#131313] border border-[#1F1F1F]">
            <div className="flex items-center justify-between mb-4">
              <div className={cn("p-3 rounded-xl bg-[#1A1A1A]/70 group-hover:scale-110 transition-transform", stat.color)}>
                <stat.icon className="h-6 w-6" />
              </div>
              <span className="text-[10px] font-black uppercase tracking-widest text-emerald-500 bg-emerald-500/10 px-2 py-1 rounded-lg">
                Live
              </span>
            </div>
            <p className="text-neutral-500 text-xs font-bold uppercase tracking-widest">{stat.name}</p>
            <h3 className="text-2xl font-black text-slate-50 mt-1 font-display">{stat.value}</h3>
          </div>
        ))}
      </div>

      {/* System Status Section */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* API Health */}
        <div className="lg:col-span-2 bg-[#131313] border border-[#1F1F1F] rounded-[6px] overflow-hidden">
          <div className="p-8 border-b border-[#1F1F1F] flex items-center justify-between bg-[#0F0F0F]">
            <div className="flex items-center gap-4">
              <Activity className="h-5 w-5 text-red-500" />
              <h3 className="text-lg font-bold text-slate-50">System Nodes Status</h3>
            </div>
            <span className="text-[10px] font-black uppercase tracking-widest text-neutral-500">Auto-Polling Active</span>
          </div>
          <div className="p-8 space-y-6">
            {stats?.nodes.map((node) => {
              const Icon = node.icon === "Globe" ? Globe : node.icon === "Database" ? Database : node.icon === "Cpu" ? Cpu : Lock;
              const isHealthy = node.status === "Healthy";
              return (
                <div key={node.name} className="flex items-center justify-between p-4 bg-[#1A1A1A]/50 rounded-[6px] border border-[#1F1F1F] group hover:bg-[#1A1A1A] transition-all">
                  <div className="flex items-center gap-4">
                    <div className="h-10 w-10 rounded-xl bg-[#1A1A1A] flex items-center justify-center group-hover:bg-neutral-700 transition-colors">
                      <Icon className="h-5 w-5 text-neutral-400" />
                    </div>
                    <div>
                      <h4 className="text-sm font-bold text-slate-50">{node.name}</h4>
                      <p className="text-[10px] font-black uppercase text-neutral-500 tracking-widest">Load: {node.load}</p>
                    </div>
                  </div>
                  <div className={cn(
                    "flex items-center gap-2 px-3 py-1.5 rounded-full border",
                    isHealthy ? "bg-emerald-500/10 border-emerald-500/20" : "bg-amber-500/10 border-amber-500/20"
                  )}>
                    <div className={cn("h-1.5 w-1.5 rounded-full", isHealthy ? "bg-emerald-500" : "bg-amber-500")} />
                    <span className={cn("text-[10px] font-bold uppercase", isHealthy ? "text-emerald-500" : "text-amber-500")}>
                      {node.status}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Global Config */}
        <div className="bg-[#131313] border border-[#1F1F1F]">
          <div className="p-8 border-b border-[#1F1F1F] flex items-center gap-4 bg-[#0F0F0F]">
            <Settings className="h-5 w-5 text-primary" />
            <h3 className="text-lg font-bold text-slate-50">Global Settings</h3>
          </div>
          <div className="p-8 flex-1 space-y-6">
            <div className="space-y-2">
              <label className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-500 pl-1">Maintenance Mode</label>
              <button
                onClick={handleToggleMaintenance}
                disabled={saving}
                className={cn(
                  "w-full flex items-center justify-between p-4 rounded-[6px] border transition-all",
                  config?.maintenance_mode
                    ? "bg-red-500/10 border-red-500/30 text-red-500"
                    : "bg-[#1A1A1A]/70 border-[#1F1F1F] text-neutral-600 hover:bg-[#1A1A1A]"
                )}
              >
                <span className="text-sm font-medium">
                  {config?.maintenance_mode ? "Restricting Access" : "Standard Operations"}
                </span>
                <div className={cn(
                  "h-6 w-11 rounded-full relative transition-colors duration-300",
                  config?.maintenance_mode ? "bg-red-500" : "bg-neutral-700"
                )}>
                  <div className={cn(
                    "absolute top-1 h-4 w-4 bg-white rounded-full transition-all duration-300 shadow-sm",
                    config?.maintenance_mode ? "left-6" : "left-1"
                  )} />
                </div>
              </button>
            </div>

            <div className="space-y-2">
              <label className="text-[10px] font-black uppercase tracking-[0.2em] text-neutral-500 pl-1">Global Notification</label>
              <textarea
                value={notification}
                onChange={(e) => setNotification(e.target.value)}
                className="w-full h-32 bg-[#1A1A1A]/70 border border-[#1F1F1F] rounded-[6px] p-4 text-xs font-medium text-neutral-300 focus:outline-none focus:ring-1 focus:ring-red-500/30 transition-all placeholder:text-neutral-600"
                placeholder="Message to display across all dashboards..."
              />
            </div>

            <button
              onClick={handleBroadcast}
              disabled={saving}
              className="w-full py-4 bg-primary rounded-[6px] font-black uppercase tracking-widest text-xs shadow-xl shadow-primary/20 hover:brightness-110 active:scale-[0.98] transition-all disabled:opacity-50"
            >
              {saving ? "Processing..." : "Broadcast Message"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
