import {createRoot} from 'react-dom/client';
import {FridayGlassesApp} from './App';
import {start} from './link';
import './styles.css';

const mount = document.getElementById('root');
if (mount == null) throw new Error('Missing application mount element');
createRoot(mount).render(<FridayGlassesApp />);
void start();
