import React from 'react';
import {Composition, Still} from 'remotion';
import {Pipeline} from './Pipeline';
import {Avatar, AvatarLoop, Banner, Logo} from './Stills';

export const RemotionRoot: React.FC = () => (
  <>
    <Still id="Logo" component={Logo} width={512} height={512} />
    <Still id="Banner" component={Banner} width={1280} height={640} />
    <Still id="Avatar" component={Avatar} width={640} height={640} />
    <Composition id="AvatarLoop" component={AvatarLoop} durationInFrames={90} fps={30} width={800} height={800} />
    <Composition id="Pipeline" component={Pipeline} durationInFrames={260} fps={30} width={1200} height={600} />
  </>
);
