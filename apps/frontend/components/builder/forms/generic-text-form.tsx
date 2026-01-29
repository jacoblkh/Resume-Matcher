import React from 'react';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import DOMPurify from 'dompurify';

interface GenericTextFormProps {
  value: string;
  onChange: (value: string) => void;
  label?: string;
  placeholder?: string;
}

/**
 * Generic Text Form Component
 *
 * Used for TEXT type sections (like Summary).
 * Renders a single textarea for text content.
 */
export const GenericTextForm: React.FC<GenericTextFormProps> = ({
  value,
  onChange,
  label = 'Content',
  placeholder = 'Enter text content...',
}) => {
  // Explicitly allow Enter key to create newlines
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter') {
      e.stopPropagation();
    }
  };

  // Function to sanitize output to prevent XSS
  const sanitizeOutput = (input: string): string => {
    // Additional validation: check length and type
    if (typeof input !== 'string' || input.length > 1000) {
      throw new Error('Input must be a string and less than 1000 characters.');
    }
    // Use DOMPurify to sanitize the input
    return DOMPurify.sanitize(input);
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const sanitizedValue = sanitizeOutput(e.target.value);
    onChange(sanitizedValue);
  };

  return (
    <div className="space-y-2">
      <Label className="font-mono text-xs uppercase tracking-wider text-gray-500">{label}</Label>
      <Textarea
        value={value ? sanitizeOutput(value) : ''}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className="min-h-[150px] text-black rounded-none border-black focus-visible:ring-0 focus-visible:ring-offset-0 focus-visible:border-blue-700 bg-white"
      />
    </div>
  );
};