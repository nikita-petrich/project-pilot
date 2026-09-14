import React from 'react';
import {AbsoluteFill, Easing, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {Mark} from './Mark';
import {Wordmark} from './Stills';
import {colors, dotGrid, mono, sans} from './theme';

type Outcome = {kind: 'rule'; reason: string} | {kind: 'llm'; score: number; note?: string};
type Listing = {title: string; meta: string; outcome: Outcome};

// Fictional listings; the one match mirrors the card format in README.md.
const LISTINGS: Listing[] = [
  {title: 'React Frontend für Versicherungsportal', meta: 'Köln · hybrid', outcome: {kind: 'llm', score: 52}},
  {title: 'SAP ABAP Entwickler (m/w/d)', meta: 'München · onsite', outcome: {kind: 'rule', reason: 'blacklist · SAP'}},
  {title: 'Senior Backend Entwickler (Node.js)', meta: 'Frankfurt · 100% remote', outcome: {kind: 'llm', score: 87}},
  {title: 'Werkstudent Online-Marketing', meta: 'Berlin · onsite', outcome: {kind: 'rule', reason: 'blacklist · Werkstudent'}},
  {title: 'Java Microservices Migration', meta: 'Hamburg · remote', outcome: {kind: 'llm', score: 34, note: 'no-go · Java'}},
];

const THRESHOLD = 60;
const MATCH_INDEX = 2;

const T = {
  rowsIn: 8,
  rules: 52,
  llm: 96,
  highlight: 150,
  card: 158,
  press: 196,
  caption: 206,
} as const;

const STAGES = [
  {label: 'fetch', from: 0, to: T.rules},
  {label: 'hard rules · 0 tokens', from: T.rules, to: T.llm},
  {label: 'LLM match', from: T.llm, to: T.card},
  {label: 'Telegram + Claude', from: T.card, to: Infinity},
];

const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

const StageBar: React.FC<{frame: number}> = ({frame}) => (
  <div style={{display: 'flex', alignItems: 'center', gap: 12}}>
    {STAGES.map((stage, i) => {
      const active = frame >= stage.from && frame < stage.to;
      const done = frame >= stage.to;
      return (
        <React.Fragment key={stage.label}>
          {i > 0 && <div style={{color: done || active ? colors.teal : colors.border, fontSize: 20}}>→</div>}
          <div
            style={{
              fontFamily: mono,
              fontSize: 17,
              padding: '7px 14px',
              borderRadius: 999,
              color: active ? colors.bg : done ? colors.teal : colors.muted,
              background: active ? colors.teal : done ? colors.tealSoft : 'transparent',
              border: `1px solid ${active || done ? colors.teal : colors.border}`,
            }}
          >
            {i + 1} {stage.label}
          </div>
        </React.Fragment>
      );
    })}
  </div>
);

const Status: React.FC<{listing: Listing; index: number; frame: number}> = ({listing, index, frame}) => {
  const {outcome} = listing;
  const rulesAt = T.rules + 8 + index * 6;
  const rulesIn = interpolate(frame, [rulesAt, rulesAt + 8], [0, 1], clamp);

  if (outcome.kind === 'rule') {
    return (
      <div style={{opacity: rulesIn, fontFamily: mono, fontSize: 15, color: colors.red, textAlign: 'right'}}>✕ {outcome.reason}</div>
    );
  }

  const llmOrder = LISTINGS.filter((l) => l.outcome.kind === 'llm').indexOf(listing);
  const llmAt = T.llm + 6 + llmOrder * 14;
  const count = interpolate(frame, [llmAt, llmAt + 22], [0, outcome.score], {...clamp, easing: Easing.out(Easing.cubic)});
  // Label and color follow the counter, so the row flips to "match" as it crosses the threshold.
  const isMatch = Math.round(count) >= THRESHOLD;
  const scoreColor = isMatch ? colors.amber : colors.muted;

  if (frame < llmAt) {
    return <div style={{opacity: rulesIn, fontFamily: mono, fontSize: 15, color: colors.teal, textAlign: 'right'}}>✓ rules</div>;
  }

  return (
    <div style={{display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6}}>
      <div style={{fontFamily: mono, fontSize: 15, color: outcome.note ? colors.red : scoreColor}}>
        {outcome.note ? `✕ ${outcome.note}` : isMatch ? 'match' : 'below threshold'} ·{' '}
        <span style={{fontWeight: 600, color: scoreColor}}>{Math.round(count)}</span>
      </div>
      <div style={{position: 'relative', width: 150, height: 5, borderRadius: 3, background: colors.border}}>
        <div style={{width: `${count}%`, height: '100%', borderRadius: 3, background: scoreColor}} />
        <div style={{position: 'absolute', left: `${THRESHOLD}%`, top: -4, width: 2, height: 13, background: colors.text, opacity: 0.6}} />
      </div>
    </div>
  );
};

const ListingRow: React.FC<{listing: Listing; index: number}> = ({listing, index}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame: frame - (T.rowsIn + index * 7), fps, config: {damping: 16}});
  const dropped = listing.outcome.kind === 'rule' || (listing.outcome.kind === 'llm' && listing.outcome.score < THRESHOLD);
  const settled = listing.outcome.kind === 'rule' ? T.llm : T.highlight;
  const dim = dropped ? interpolate(frame, [settled, settled + 12], [1, 0.45], clamp) : 1;
  const glow = index === MATCH_INDEX ? interpolate(frame, [T.highlight, T.highlight + 10], [0, 1], clamp) : 0;

  return (
    <div
      style={{
        opacity: enter * dim,
        transform: `translateX(${(1 - enter) * -40}px)`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        height: 70,
        padding: '0 20px',
        borderRadius: 14,
        background: colors.bgRaised,
        border: `1.5px solid ${glow > 0 ? `rgba(251,191,36,${glow})` : colors.border}`,
        boxShadow: `0 0 ${28 * glow}px rgba(251,191,36,${0.35 * glow})`,
      }}
    >
      <div>
        <div style={{fontSize: 19, fontWeight: 600, color: colors.text}}>{listing.title}</div>
        <div style={{fontSize: 14, color: colors.muted, marginTop: 4}}>{listing.meta}</div>
      </div>
      <Status listing={listing} index={index} frame={frame} />
    </div>
  );
};

const CARD_LINES = [
  '🎯 Senior Backend Entwickler (Node.js) · 87/100',
  '🏢 Muster Digital GmbH · Agency',
  '📍 Frankfurt am Main · 🏠 100%',
  '📅 01.10.2026 · ⏳ 6 mo · 🕒 4 min ago',
  '✅ Fits: Node.js, REST, Docker, PostgreSQL',
  '⚠️ Risks: agency listing, end client unnamed',
];

const Button: React.FC<{label: string; pressed?: number; accent?: boolean}> = ({label, pressed = 0, accent = false}) => (
  <div
    style={{
      flex: 1,
      height: 44,
      borderRadius: 10,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      fontSize: 16,
      fontWeight: 500,
      color: accent ? colors.teal : colors.text,
      background: pressed > 0 ? `rgba(45,212,191,${0.12 + 0.2 * pressed})` : colors.telegramButton,
      transform: `scale(${1 - 0.05 * pressed})`,
    }}
  >
    {label}
  </div>
);

const TelegramCard: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const enter = spring({frame: frame - T.card, fps, config: {damping: 14}});
  const press = interpolate(frame, [T.press, T.press + 4, T.press + 10], [0, 1, 0], clamp);
  const caption = spring({frame: frame - T.caption, fps, config: {damping: 18}});

  return (
    <div style={{opacity: enter, transform: `translateY(${(1 - enter) * 60}px)`}}>
      <div style={{display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12}}>
        <div style={{width: 40, height: 40, borderRadius: '50%', overflow: 'hidden'}}>
          <Mark size={40} radius={0} />
        </div>
        <div>
          <div style={{fontSize: 17, fontWeight: 600, color: colors.text}}>Project Pilot</div>
          <div style={{fontSize: 13, color: colors.muted}}>bot · now</div>
        </div>
      </div>
      <div style={{background: colors.telegramBubble, borderRadius: 18, padding: '18px 20px', fontSize: 17, lineHeight: 1.6, color: colors.text}}>
        {CARD_LINES.map((line) => (
          <div key={line}>{line}</div>
        ))}
      </div>
      <div style={{display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6}}>
        <div style={{display: 'flex'}}>
          <Button label="Projektbeschreibung öffnen" />
        </div>
        <div style={{display: 'flex', gap: 6}}>
          <Button label="Bewerben" pressed={press} accent />
          <Button label="Ablehnen" />
        </div>
      </div>
      <div
        style={{
          opacity: caption,
          transform: `translateY(${(1 - caption) * 12}px)`,
          marginTop: 18,
          fontFamily: mono,
          fontSize: 16,
          color: colors.teal,
        }}
      >
        → Claude chat, card prefilled, one tap to draft
      </div>
    </div>
  );
};

/** The README animation: listings in, rules and LLM thin them out, one card goes out. */
export const Pipeline: React.FC = () => {
  const frame = useCurrentFrame();
  const {durationInFrames} = useVideoConfig();
  const fadeOut = interpolate(frame, [durationInFrames - 8, durationInFrames - 1], [1, 0], clamp);
  const sweep = (frame / durationInFrames) * 720;

  return (
    <AbsoluteFill style={{...dotGrid, fontFamily: sans, padding: '28px 40px', opacity: fadeOut}}>
      <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between'}}>
        <div style={{display: 'flex', alignItems: 'center', gap: 14}}>
          <Mark size={46} sweep={sweep} ping={(frame / 45) % 1} />
          <Wordmark fontSize={30} />
        </div>
        <div style={{fontFamily: mono, fontSize: 16, color: colors.muted}}>scan · every 15 min</div>
      </div>

      <div style={{marginTop: 22}}>
        <StageBar frame={frame} />
      </div>

      <div style={{display: 'flex', gap: 36, marginTop: 24}}>
        <div style={{width: 600, display: 'flex', flexDirection: 'column', gap: 9}}>
          {LISTINGS.map((listing, i) => (
            <ListingRow key={listing.title} listing={listing} index={i} />
          ))}
        </div>
        <div style={{flex: 1}}>
          <TelegramCard />
        </div>
      </div>
    </AbsoluteFill>
  );
};
