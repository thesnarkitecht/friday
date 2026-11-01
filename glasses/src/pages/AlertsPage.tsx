import {ListItem, Page, VerticalList} from '@wearables-ui-toolkit/mrbd';
import {useLink} from '../link';

export function AlertsPage() {
  const {alerts} = useLink();
  return (
    <Page headerText="Alerts" enableSystemBarInset={false}>
      <VerticalList insetForHeader ariaLabel="Alerts">
        {alerts.length === 0 ? <ListItem title="All clear" subtitle="Weather, quakes, reminders and GitHub show up here." subtitleMaxLines={2} /> : null}
        {alerts.map(a => (
          <ListItem key={a.id} title={a.text} subtitle={a.kind}
            timestamp={new Date(a.at).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})} />
        ))}
      </VerticalList>
    </Page>
  );
}
