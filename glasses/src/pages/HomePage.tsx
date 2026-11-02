import {ListItem, Page, StatusIndicatorType, VerticalList} from '@wearables-ui-toolkit/mrbd';
import aiConsumerFilled from '@wearables-ui-toolkit/icons/svg/aiconsumer__filled.svg';
import aiMemoryFilled from '@wearables-ui-toolkit/icons/svg/aimemory__filled.svg';
import bellRingFilled from '@wearables-ui-toolkit/icons/svg/bellring__filled.svg';
import {useNavigate} from 'react-router-dom';
import {useLink} from '../link';

const STATUS = {connected: 'Paired', pairing: 'Pairing…', offline: 'Reconnecting…', unpaired: 'Not paired'} as const;

export function HomePage() {
  const navigate = useNavigate();
  const {status, lines, facts, alerts, screen} = useLink();
  const last = [...lines].reverse().find(l => l.who === 'friday');

  return (
    <Page headerText="FRIDAY" headerMetadata={STATUS[status]} enableSystemBarInset={false}>
      <VerticalList insetForHeader ariaLabel="FRIDAY">
        {status === 'unpaired' ? (
          <ListItem
            title="Pair with your phone"
            subtitle="On your iPhone, open FRIDAY › Settings › Pair glasses and scan the code."
            subtitleMaxLines={3}
          />
        ) : null}
        <ListItem
          title="Talk"
          subtitle={last?.text ?? 'Say "Friday…" or tap to type'}
          subtitleMaxLines={2}
          icon={aiConsumerFilled}
          onClick={() => navigate('/talk')}
        />
        {screen ? (
          <ListItem title="iPhone screen" subtitle="Sharing now" onClick={() => navigate('/screen')} />
        ) : null}
        <ListItem
          title="Memory"
          subtitle={facts.length ? `${facts.length} ${facts.length === 1 ? 'thing' : 'things'} I know about you` : 'Nothing saved yet'}
          icon={aiMemoryFilled}
          onClick={() => navigate('/memory')}
        />
        <ListItem
          title="Alerts"
          subtitle={alerts[0]?.text ?? 'All clear'}
          subtitleMaxLines={2}
          icon={bellRingFilled}
          avatarStatusIndicator={alerts.length ? StatusIndicatorType.UNREAD : undefined}
          onClick={() => navigate('/alerts')}
        />
      </VerticalList>
    </Page>
  );
}
