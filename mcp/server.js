#!/usr/bin/env node
// Korea Interface Archive — MCP server.
// Gives AI clients read-only access to the archive's observations.
// Data source: KIA_DATA (local path or URL), else the public file on GitHub.

import { readFile } from "node:fs/promises";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const DEFAULT_BASE = "https://raw.githubusercontent.com/yunglordsimens/interface-archiv/main/";
const SOURCE = process.env.KIA_DATA || DEFAULT_BASE + "data/observations.json";
const MEDIA_BASE = process.env.KIA_MEDIA_BASE || DEFAULT_BASE;

let cache = null;
async function load() {
  if (cache) return cache;
  const text = /^https?:\/\//.test(SOURCE)
    ? await (await fetch(SOURCE)).text()
    : await readFile(SOURCE, "utf8");
  cache = JSON.parse(text);
  return cache;
}

const NOTE =
  "Levels are kept apart: 'observed' is what the author saw, 'confirmed' is backed by the listed sources, " +
  "'interpretation' and 'open' are not established facts. Cite the observation id and its sources when you use it.";

function withMedia(o) {
  return { ...o, media: (o.media || []).map((m) => ({ ...m, url: new URL(m.src, MEDIA_BASE).href })) };
}
function summary(o) {
  return {
    id: o.id, title: o.title, category: o.category, country: o.country,
    subject: o.subject || null, layers: o.layers || [], barrier: o.barrier || null, service: o.service, place: o.place, date: o.date, scenario: o.scenario,
    patterns: o.breakdown?.patterns || [],
  };
}
function text(obj) {
  return { content: [{ type: "text", text: JSON.stringify(obj, null, 2) }] };
}
function haystack(o) {
  const b = o.breakdown || {};
  return [
    o.title, o.service, o.place, o.category, o.scenario,
    ...(o.observed || []), ...(o.interpretation || []), ...(o.open || []),
    ...(b.steps || []).flatMap((s) => [s.action, s.text_ko, s.text_en]),
    ...(b.elements || []).flatMap((e) => [e.name, e.role, e.text_ko, e.text_en]),
    ...(b.patterns || []), o.subject || "",
  ].join(" ").toLowerCase();
}

const server = new McpServer({ name: "korea-interface-archive", version: "0.2.0" });

server.registerTool(
  "search_observations",
  {
    title: "Search observations",
    description:
      "Search documented Korean digital and physical interfaces. Matches title, service, place, observations, " +
      "interface steps and elements (Korean original text and English translation). Returns short summaries.",
    inputSchema: {
      query: z.string().optional().describe("Words to look for, in English or Korean"),
      category: z.string().optional().describe("Digital, Physical, Services or Systems"),
      pattern: z.string().optional().describe("Pattern id, see list_patterns"),
      subject: z.string().optional().describe("Subject id (a specific store, app, device or service), see list_subjects"),
      layer: z.enum(["discovery", "communication", "payment", "delivery", "unattended"]).optional().describe("Infrastructure layer"),
      barriers_only: z.boolean().optional().describe("Only things a foreigner could not do"),
    },
  },
  async ({ query, category, pattern, subject, layer, barriers_only }) => {
    const d = await load();
    const q = (query || "").toLowerCase().trim();
    const words = q ? q.split(/\s+/) : [];
    const hits = d.observations.filter((o) =>
      (!category || o.category.toLowerCase() === category.toLowerCase()) &&
      (!pattern || (o.breakdown?.patterns || []).includes(pattern)) &&
      (!subject || o.subject === subject) &&
      (!layer || (o.layers || []).includes(layer)) &&
      (!barriers_only || !!o.barrier) &&
      words.every((w) => haystack(o).includes(w)));
    return text({ count: hits.length, results: hits.map(summary), note: NOTE });
  }
);

server.registerTool(
  "get_observation",
  {
    title: "Get observation",
    description:
      "Full record of one observation: access requirements, observed facts, confirmed facts with sources, " +
      "interpretation, open questions, step-by-step breakdown, elements with original Korean text, patterns, " +
      "and image URLs.",
    inputSchema: { id: z.string().describe("Observation id, e.g. 001") },
  },
  async ({ id }) => {
    const d = await load();
    const o = d.observations.find((x) => x.id === id);
    if (!o) return { isError: true, content: [{ type: "text", text: `No observation with id ${id}. Use search_observations to find ids.` }] };
    return text({ observation: withMedia(o), subject: o.subject ? { id: o.subject, ...(d.subjects?.[o.subject] || {}) } : null, patterns: Object.fromEntries((o.breakdown?.patterns || []).map((p) => [p, d.patterns?.[p]])), note: NOTE });
  }
);

server.registerTool(
  "list_patterns",
  {
    title: "List patterns",
    description: "Recurring interface solutions found in the archive, with a description and the ids of observations where each was seen.",
    inputSchema: {},
  },
  async () => {
    const d = await load();
    const list = Object.entries(d.patterns || {}).map(([id, p]) => ({
      id, ...p,
      observations: d.observations.filter((o) => (o.breakdown?.patterns || []).includes(id)).map((o) => o.id),
    }));
    return text({ count: list.length, patterns: list });
  }
);

server.registerTool(
  "list_subjects",
  {
    title: "List subjects",
    description: "The specific stores, apps, websites, devices and services documented in the archive, with the ids of observations about each.",
    inputSchema: {},
  },
  async () => {
    const d = await load();
    const list = Object.entries(d.subjects || {}).map(([id, s]) => ({
      id, ...s, observations: d.observations.filter((o) => o.subject === id).map((o) => o.id),
    }));
    return text({ count: list.length, subjects: list });
  }
);

server.registerTool(
  "access_requirements",
  {
    title: "Access requirements",
    description:
      "What it takes to use each documented interface (physical presence, Korean phone number, residence card, " +
      "Korean card or address, identity verification). Optionally filter by a word such as 'phone' or 'ARC'.",
    inputSchema: { requirement: z.string().optional().describe("Word to filter requirements by") },
  },
  async ({ requirement }) => {
    const d = await load();
    const r = (requirement || "").toLowerCase();
    const rows = d.observations
      .filter((o) => o.access)
      .filter((o) => !r || [...(o.access.requires || []), ...(o.access.unverified || [])].join(" ").toLowerCase().includes(r))
      .map((o) => ({ id: o.id, title: o.title, requires: o.access.requires, to_check: o.access.unverified || [] }));
    return text({ count: rows.length, results: rows, author_access: d.author_access || [] });
  }
);

await server.connect(new StdioServerTransport());
