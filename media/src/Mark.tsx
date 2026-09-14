import React from 'react';
import {colors} from './theme';

const CENTER = 50;

/** Point on a circle around the center; 0° is north, angles run clockwise. */
const polar = (radius: number, degrees: number): [number, number] => {
  const rad = (degrees * Math.PI) / 180;
  return [CENTER + radius * Math.sin(rad), CENTER - radius * Math.cos(rad)];
};

const wedge = (radius: number, from: number, to: number): string => {
  const [x0, y0] = polar(radius, from);
  const [x1, y1] = polar(radius, to);
  return `M${CENTER} ${CENTER} L${x0} ${y0} A${radius} ${radius} 0 0 1 ${x1} ${y1} Z`;
};

type MarkProps = {
  size: number;
  /** Leading edge of the radar sweep, in degrees. */
  sweep?: number;
  /** 0..1 progress of the ping ring around the match blip. */
  ping?: number;
  /** Corner radius of the background tile in viewBox units; null draws no tile. */
  radius?: number | null;
};

/**
 * The project-pilot mark: a navigation arrow at the center of a radar that has
 * just caught one blip — the match.
 */
export const Mark: React.FC<MarkProps> = ({size, sweep = 38, ping = 0.4, radius = 22}) => {
  const [blipX, blipY] = polar(42, 45);
  return (
    <svg width={size} height={size} viewBox="0 0 100 100">
      <defs>
        <radialGradient id="pp-tile" cx="50%" cy="38%" r="70%">
          <stop offset="0%" stopColor="#14254A" />
          <stop offset="100%" stopColor={colors.bg} />
        </radialGradient>
        <linearGradient id="pp-arrow" x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor={colors.teal} />
          <stop offset="100%" stopColor={colors.mint} />
        </linearGradient>
      </defs>

      {radius !== null && <rect width="100" height="100" rx={radius} fill="url(#pp-tile)" />}

      {[42, 30, 18].map((r) => (
        <circle key={r} cx={CENTER} cy={CENTER} r={r} fill="none" stroke={colors.teal} strokeOpacity={0.22} strokeWidth={0.9} />
      ))}
      <line x1={8} y1={CENTER} x2={92} y2={CENTER} stroke={colors.teal} strokeOpacity={0.1} strokeWidth={0.6} />
      <line x1={CENTER} y1={8} x2={CENTER} y2={92} stroke={colors.teal} strokeOpacity={0.1} strokeWidth={0.6} />

      <path d={wedge(42, sweep - 70, sweep)} fill={colors.teal} fillOpacity={0.06} />
      <path d={wedge(42, sweep - 38, sweep)} fill={colors.teal} fillOpacity={0.1} />
      <path d={wedge(42, sweep - 14, sweep)} fill={colors.teal} fillOpacity={0.16} />
      <line x1={CENTER} y1={CENTER} x2={polar(42, sweep)[0]} y2={polar(42, sweep)[1]} stroke={colors.teal} strokeOpacity={0.7} strokeWidth={0.8} />

      <circle cx={blipX} cy={blipY} r={2.6 + ping * 9} fill="none" stroke={colors.amber} strokeWidth={1.1} strokeOpacity={1 - ping} />
      <circle cx={blipX} cy={blipY} r={2.8} fill={colors.amber} />

      <g transform={`rotate(45 ${CENTER} ${CENTER})`}>
        <path d="M50 18 L69 66 L50 56 L31 66 Z" fill="url(#pp-arrow)" />
        <path d="M50 18 L50 56 L31 66 Z" fill="#000" fillOpacity={0.14} />
      </g>
    </svg>
  );
};
