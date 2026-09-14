import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {Mark} from './Mark';
import {colors, dotGrid, mono, sans} from './theme';

/** Square app icon on a transparent background. */
export const Logo: React.FC = () => (
  <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
    <Mark size={512} />
  </AbsoluteFill>
);

/** Telegram crops profile photos to a circle, so the tile runs edge to edge. */
export const Avatar: React.FC = () => (
  <AbsoluteFill>
    <Mark size={640} radius={0} />
  </AbsoluteFill>
);

/** Seamless loop of the avatar: one full sweep, one ping. */
export const AvatarLoop: React.FC = () => {
  const frame = useCurrentFrame();
  const {durationInFrames, width} = useVideoConfig();
  const progress = frame / durationInFrames;
  return (
    <AbsoluteFill>
      <Mark size={width} radius={0} sweep={progress * 360} ping={(progress * 2) % 1} />
    </AbsoluteFill>
  );
};

export const Wordmark: React.FC<{fontSize: number}> = ({fontSize}) => (
  <div style={{fontFamily: sans, fontWeight: 800, fontSize, letterSpacing: -fontSize * 0.035, color: colors.text, lineHeight: 1}}>
    project<span style={{color: colors.teal}}>-pilot</span>
  </div>
);

const CHIPS = ['Python 3.13', 'asyncio', 'mypy --strict', 'PostgreSQL', 'MCP', 'Telegram'];

/** README hero and GitHub social preview (1280×640). */
export const Banner: React.FC = () => {
  // Stills render frame 0; interpolate keeps the ring layout declarative.
  const ring = (i: number) => interpolate(i, [0, 3], [260, 620]);
  return (
    <AbsoluteFill style={{...dotGrid, fontFamily: sans}}>
      <AbsoluteFill style={{background: `radial-gradient(circle at 18% 50%, rgba(45,212,191,0.16), transparent 45%)`}} />
      {[0, 1, 2, 3].map((i) => (
        <div
          key={i}
          style={{
            position: 'absolute',
            left: 240 - ring(i) / 2,
            top: 320 - ring(i) / 2,
            width: ring(i),
            height: ring(i),
            borderRadius: '50%',
            border: `1.5px solid rgba(45,212,191,${0.14 - i * 0.03})`,
          }}
        />
      ))}

      <div style={{position: 'absolute', left: 120, top: 200}}>
        <Mark size={240} />
      </div>

      <div style={{position: 'absolute', left: 430, top: 150, right: 90}}>
        <Wordmark fontSize={104} />
        <div style={{marginTop: 30, fontSize: 34, lineHeight: 1.35, color: colors.text, fontWeight: 500}}>
          New freelance projects, judged in minutes.
        </div>
        <div style={{marginTop: 12, fontSize: 24, lineHeight: 1.45, color: colors.muted}}>
          Scans the board, checks every fresh listing against your profile — hard rules, then an LLM — and pushes only the real
          matches to Telegram and Claude.
        </div>
        <div style={{marginTop: 34, display: 'flex', flexWrap: 'wrap', gap: 10}}>
          {CHIPS.map((chip) => (
            <div
              key={chip}
              style={{
                fontFamily: mono,
                fontSize: 17,
                color: colors.teal,
                background: colors.tealSoft,
                border: `1px solid rgba(45,212,191,0.3)`,
                borderRadius: 999,
                padding: '5px 12px',
              }}
            >
              {chip}
            </div>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};
