"use client";

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

export default function SenzuChart({
  data,
}: {
  data: { min: number; senzu: number }[];
}) {
  return (
    <ResponsiveContainer width="100%" height={120}>
      <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
        <XAxis
          dataKey="min"
          tick={{ fontSize: 9, fill: "#6b7280" }}
          tickLine={false}
          axisLine={{ stroke: "#374151" }}
          unit="m"
        />
        <YAxis
          tick={{ fontSize: 9, fill: "#6b7280" }}
          tickLine={false}
          axisLine={false}
          width={36}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: "#111827",
            border: "1px solid #374151",
            borderRadius: "6px",
            fontSize: "11px",
          }}
          labelFormatter={(v) => `${v}m`}
        />
        <Line
          type="monotone"
          dataKey="senzu"
          stroke="#f59e0b"
          strokeWidth={2}
          dot={false}
          name="Senzu/h"
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
