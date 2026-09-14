import {loadFont as loadInter} from '@remotion/google-fonts/Inter';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';

export const colors = {
  bg: '#070B14',
  bgRaised: '#0E1628',
  border: '#1D2942',
  text: '#E6EDF7',
  muted: '#8B98B3',
  teal: '#2DD4BF',
  tealSoft: 'rgba(45, 212, 191, 0.14)',
  mint: '#A7F3D0',
  amber: '#FBBF24',
  red: '#F87171',
  telegramBubble: '#182533',
  telegramButton: 'rgba(255, 255, 255, 0.08)',
} as const;

export const {fontFamily: sans} = loadInter('normal', {
  weights: ['400', '500', '600', '800'],
  subsets: ['latin'],
});

export const {fontFamily: mono} = loadMono('normal', {
  weights: ['400', '600'],
  subsets: ['latin'],
});

/** Subtle dot grid used behind every composition. */
export const dotGrid = {
  backgroundColor: colors.bg,
  backgroundImage: `radial-gradient(${colors.border} 1.2px, transparent 1.2px)`,
  backgroundSize: '28px 28px',
} as const;
