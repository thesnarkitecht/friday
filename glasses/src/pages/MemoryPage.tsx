import {ListItem, Page, VerticalList} from '@wearables-ui-toolkit/mrbd';
import {useLink} from '../link';

export function MemoryPage() {
  const {facts} = useLink();
  return (
    <Page headerText="Memory" headerMetadata={`${facts.length} facts`} enableSystemBarInset={false}>
      <VerticalList insetForHeader ariaLabel="What FRIDAY remembers">
        {facts.length === 0 ? <ListItem title="Nothing yet" subtitle='Try "Friday, remember…"' /> : null}
        {facts.map(f => {
          const m = /^\[(\w+)\]\s*(.*)$/.exec(f);  // server sends "[category] fact"
          return <ListItem key={f} title={m ? m[2] : f} subtitle={m?.[1]} subtitleMaxLines={1} />;
        })}
      </VerticalList>
    </Page>
  );
}
