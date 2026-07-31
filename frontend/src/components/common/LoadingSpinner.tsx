/** Loading indicator. Always given a specific label - "Loading..." tells nobody what. */

const sizeClasses = {
  sm: 'h-5 w-5 border-2',
  md: 'h-8 w-8 border-[3px]',
  lg: 'h-12 w-12 border-4',
} as const;

export interface LoadingSpinnerProps {
  size?: keyof typeof sizeClasses;
  label?: string;
}

export const LoadingSpinner = ({ size = 'md', label = 'Loading...' }: LoadingSpinnerProps) => (
  <div className="flex flex-col items-center justify-center gap-3 py-8 text-slate-500 dark:text-slate-400">
    <div
      className={`${sizeClasses[size]} animate-spin rounded-full border-blue-600 border-t-transparent`}
    />
    <p className="text-sm font-medium">{label}</p>
  </div>
);

export default LoadingSpinner;
