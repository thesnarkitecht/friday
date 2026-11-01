import {Page} from '@wearables-ui-toolkit/mrbd';
import {Navigate} from 'react-router-dom';
import {useLink} from '../link';

export function ScreenPage() {
  const {screen} = useLink();
  if (!screen) return <Navigate to="/" replace />;
  return (
    <Page headerText="iPhone" headerMetadata="Screen sharing" enableSystemBarInset={false}>
      <div className="screen-share"><img src={screen} alt="Your iPhone screen" /></div>
    </Page>
  );
}
