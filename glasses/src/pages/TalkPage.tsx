import {
  Button,
  ButtonRail,
  Container,
  CornerRadius,
  InputTextView,
  MaterialLibrary,
  Page,
  TailShapeProvider,
  TextColor,
  TextStyle,
  TextView,
  VerticalList,
} from '@wearables-ui-toolkit/mrbd';
import cameraFilled from '@wearables-ui-toolkit/icons/svg/camera__filled.svg';
import {useLayoutEffect, useMemo, useRef, useState} from 'react';
import {say, useLink, type Line} from '../link';

const SUGGESTIONS = ["What's next today?", 'Any alerts near me?', 'What do you know about me?'];

// Canvas materials need resolved colors, not var() references (same as Meta's messaging sample).
function token(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function Bubble({line, last}: {line: Line; last: boolean}) {
  const mine = line.who !== 'friday';
  const shape = useMemo(() => new TailShapeProvider(mine ? 'right' : 'left', CornerRadius.MEDIUM), [mine]);
  const material = useMemo(() => mine
    ? MaterialLibrary.outboundMessage({
        idleFill: token('--uit-color-background-message-outbound'),
        gradientStep1: token('--uit-color-container-message-outbound-target-step1'),
        gradientStep2: token('--uit-color-container-message-outbound-target-step2'),
        gradientStep3: token('--uit-color-container-message-outbound-target-step3'),
        gradientStep4: token('--uit-color-container-message-outbound-target-step4'),
        glowTint: token('--uit-color-container-message-outbound-glow'),
      })
    : MaterialLibrary.inboundMessage(), [mine]);
  return (
    <div className={`message-row ${mine ? 'message-row--self' : ''}`}>
      <div className="message-column">
        <Container
          ariaLabel={`${mine ? 'You' : 'FRIDAY'}: ${line.text}`}
          focusable
          initialFocusEligible={last}
          material={material}
          shapeProvider={shape}>
          <div className="message-bubble-content">
            <TextView as="p" textStyle={TextStyle.BODY2}
              textColor={line.who === 'heard' ? TextColor.SECONDARY : undefined}>
              {line.text}
            </TextView>
          </div>
        </Container>
      </div>
    </div>
  );
}

export function TalkPage() {
  const {lines, thinking} = useLink();
  const shown = lines.filter(l => l.who !== 'heard');
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { endRef.current?.scrollIntoView({block: 'end'}); }, [shown.length, thinking]);

  const send = (text: string) => {
    if (!text.trim()) return;
    say(text.trim());
    setDraft('');
    setComposing(false);
  };

  return (
    <Page headerText="FRIDAY" headerMetadata={thinking ? 'Thinking…' : undefined} enableSystemBarInset={false}>
      <div className="talk-shell">
        <VerticalList insetForHeader contentClassName="message-list" ariaLabel="Conversation with FRIDAY">
          {shown.length === 0 ? (
            <TextView as="p" textStyle={TextStyle.BODY2} textColor={TextColor.SECONDARY}>
              Say "Friday…" out loud, or tap Ask.
            </TextView>
          ) : null}
          {shown.map((l, i) => <Bubble key={l.id} line={l} last={i === shown.length - 1} />)}
          <div ref={endRef} />
        </VerticalList>
        <div className="action-dock">
          {composing ? (
            <div className="draft-input">
              <InputTextView
                text={draft}
                hint="Ask FRIDAY"
                actionLabel="Send to FRIDAY"
                onTextChange={setDraft}
                onSend={send}
                inputProps={{'aria-label': 'Ask FRIDAY', autoFocus: true}}
              />
            </div>
          ) : (
            <ButtonRail centerContentWhenSmallerThanWidth={false}>
              <Button title="Ask" initialFocusEligible={false} onClick={() => setComposing(true)} />
              <Button title="Look" icon={cameraFilled} initialFocusEligible={false}
                onClick={() => send('What am I looking at?')} />
              {SUGGESTIONS.map(s => (
                <Button key={s} title={s} initialFocusEligible={false} onClick={() => send(s)} />
              ))}
            </ButtonRail>
          )}
        </div>
      </div>
    </Page>
  );
}
